# ba_meta require api 9
# coding: utf-8
"""
Custom Music Manager for BombSquad Windows
Adds file-based soundtrack support on Windows where it's normally unavailable.
"""
from __future__ import annotations
import os
import threading
import ctypes
import ctypes.wintypes
from typing import TYPE_CHECKING, Any, Callable
import babase
import bauiv1 as bui
from baclassic._music import MusicPlayer, MusicSubsystem

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Patch supports_soundtrack_entry_type at import time.
# MusicSubsystem.__init__ calls supports_soundtrack_entry_type('musicFile')
# to decide whether to set _music_player_type. This happens during app
# construction — before any Plugin.on_app_loading fires. So we must patch
# the METHOD on the CLASS here, at module load time, before the instance
# is ever created. The plugin's _install_player() then sets the player type.
# ---------------------------------------------------------------------------
_original_supports = MusicSubsystem.supports_soundtrack_entry_type

def _patched_supports(self: MusicSubsystem, entry_type: str) -> bool:
    if entry_type == 'musicFile':
        return True
    return _original_supports(self, entry_type)

MusicSubsystem.supports_soundtrack_entry_type = _patched_supports  # type: ignore[method-assign]

# ---------------------------------------------------------------------------
# Native Windows file/folder picker via ctypes
# ---------------------------------------------------------------------------
def _pick_file_windows(title: str = 'Select Music File') -> str | None:
    """Open a native Windows file picker, return path or None."""
    OFN_FILEMUSTEXIST = 0x00001000
    OFN_PATHMUSTEXIST = 0x00000800
    OFN_HIDEREADONLY  = 0x00000004
    OFN_EXPLORER      = 0x00080000
    OFN_NOCHANGEDIR   = 0x00000008

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ('lStructSize',       ctypes.wintypes.DWORD),
            ('hwndOwner',         ctypes.wintypes.HWND),
            ('hInstance',         ctypes.wintypes.HINSTANCE),
            ('lpstrFilter',       ctypes.c_wchar_p),
            ('lpstrCustomFilter', ctypes.c_wchar_p),
            ('nMaxCustFilter',    ctypes.wintypes.DWORD),
            ('nFilterIndex',      ctypes.wintypes.DWORD),
            ('lpstrFile',         ctypes.c_wchar_p),
            ('nMaxFile',          ctypes.wintypes.DWORD),
            ('lpstrFileTitle',    ctypes.c_wchar_p),
            ('nMaxFileTitle',     ctypes.wintypes.DWORD),
            ('lpstrInitialDir',   ctypes.c_wchar_p),
            ('lpstrTitle',        ctypes.c_wchar_p),
            ('Flags',             ctypes.wintypes.DWORD),
            ('nFileOffset',       ctypes.wintypes.WORD),
            ('nFileExtension',    ctypes.wintypes.WORD),
            ('lpstrDefExt',       ctypes.c_wchar_p),
            ('lCustData',         ctypes.wintypes.LPARAM),
            ('lpfnHook',          ctypes.c_void_p),
            ('lpTemplateName',    ctypes.c_wchar_p),
            ('pvReserved',        ctypes.c_void_p),
            ('dwReserved',        ctypes.wintypes.DWORD),
            ('FlagsEx',           ctypes.wintypes.DWORD),
        ]

    buf = ctypes.create_unicode_buffer(32768)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.lpstrFilter = 'WAV Audio Files\0*.wav\0All Files\0*.*\0\0'
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.cast(buf, ctypes.c_wchar_p)
    ofn.nMaxFile = len(buf)
    ofn.lpstrTitle = title
    # ИСПРАВЛЕНИЕ: Добавлен OFN_NOCHANGEDIR
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_HIDEREADONLY | OFN_EXPLORER | OFN_NOCHANGEDIR

    result = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    return buf.value if result else None

# ---------------------------------------------------------------------------
# Music player using Windows MCI (winmm.dll)
# ---------------------------------------------------------------------------
# MCI (Media Control Interface) is built into every Windows version.
# We use mciSendStringW to open/play/stop audio files in a loop thread.
# Volume is set via the "setaudio" MCI command (0-1000 scale).
_winmm = ctypes.windll.winmm

def _mci(cmd: str) -> tuple[int, str]:
    """Send an MCI command string, return (error_code, error_text)."""
    buf = ctypes.create_unicode_buffer(512)
    rc = _winmm.mciSendStringW(cmd, buf, len(buf), None)
    return rc, buf.value

class WindowsMusicPlayer(MusicPlayer):
    """Music player for Windows using WinMM MCI — always available."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._volume_val: float = 1.0  # ИСПРАВЛЕНО: было flo at
        self._alias_counter = 0

    # --- public MusicPlayer interface ---

    def on_select_entry(
        self,
        callback: Callable[[Any], None],
        current_entry: Any,
        selection_target_name: str,  # ИСПРАВЛЕНО: было select ion_target_name
    ) -> bui.MainWindow:
        return EntrySelectWindow(
            callback=callback,
            current_entry=current_entry,
            selection_target_name=selection_target_name,
        )

    def on_play(self, entry: Any) -> None:  # ИСПРАВЛЕНО: было on_p lay
        music = babase.app.classic.music
        entry_type = music.get_soundtrack_entry_type(entry)
        path = music.get_soundtrack_entry_name(entry)
        print(f'[CustomMusic] on_play: type={entry_type} path={path}')
        if entry_type == 'musicFile':
            self._start_thread([path])
        else:
            print(f'[CustomMusic] on_play: unhandled entry_type={entry_type!r}')

    def on_stop(self) -> None:
        self._stop_thread()

    def on_app_shutdown(self) -> None:
        self._stop_thread()

    def on_set_volume(self, volume: float) -> None:
        self._volume_val = volume
        # Volume change takes effect on next track open

    # --- internals ---

    def _stop_thread(self) -> None:
        with self._lock:
            self._stop_event.set()
            t = self._thread
            self._thread = None
        if t is not None and t.is_alive():
            t.join(timeout=2.0)

    def _start_thread(self, files: list[str]) -> None:
        self._stop_thread()
        self._stop_event.clear()
        vol = self._volume_val
        stop_ev = self._stop_event

        def _run() -> None:
            playlist = list(files)
            idx = 0
            while not stop_ev.is_set():
                path = playlist[idx % len(playlist)]
                idx += 1

                if not os.path.isfile(path):
                    print(f'[CustomMusic] skipping missing file: {path!r}')
                    continue

                # Each open needs a unique alias
                alias = f'cm{id(stop_ev) & 0xFFFF:04x}'  # ИСПРАВЛЕНО: было al ias
                _mci(f'close {alias}')

                # Open WAV file using waveaudio — always available on Windows
                rc, _ = _mci(f'open "{path}" type waveaudio alias {alias}')
                if rc != 0:
                    # Fallback: auto-detect
                    _mci(f'close {alias}')
                    rc, _ = _mci(f'open "{path}" alias {alias}')
                if rc != 0:
                    err_buf = ctypes.create_unicode_buffer(512)
                    _winmm.mciGetErrorStringW(rc, err_buf, len(err_buf))
                    print(f'[CustomMusic] MCI open failed rc={rc} ({err_buf.value}) {path!r}')
                    # Show user-friendly message once, then stop retrying
                    def _notify_unsupported(p: str = path) -> None:
                        bui.screenmessage(
                            f'Unsupported file: {os.path.basename(p)}\n'
                            'Use WAV or CBR MP3 (not VBR)',
                            color=(1.0, 0.4, 0.4),
                        )
                    babase.pushcall(_notify_unsupported)
                    stop_ev.wait(300.0)  # wait long — no point retrying bad file
                    continue

                # ИСПРАВЛЕНИЕ: Обернул воспроизведение в try/finally
                # Это гарантирует закрытие MCI алиаса даже при ошибке
                try:
                    # Set volume (MCI audio: 0-1000)
                    mci_vol = int(max(0.0, min(1.0, vol)) * 1000)
                    _mci(f'setaudio {alias} volume to {mci_vol}')

                    # Get duration
                    _mci(f'set {alias} time format milliseconds')
                    _, dur_str = _mci(f'status {alias} length')
                    try:
                        duration_ms = int(dur_str.strip())  # ИСПРАВЛЕНО: было str ip()
                    except ValueError:
                        duration_ms = 300_000  # fallback 5 min

                    _mci(f'play {alias}')
                    print(f'[CustomMusic] playing: {os.path.basename(path)} ({duration_ms}ms)')

                    # Wait for playback to finish or stop signal
                    elapsed = 0
                    interval = 200
                    while elapsed < duration_ms and not stop_ev.is_set():
                        stop_ev.wait(interval / 1000.0)
                        elapsed += interval
                finally:
                    # ГАРАНТИРОВАННО закрываем алиас
                    _mci(f'stop {alias}')
                    _mci(f'close {alias}')  # ИСПРАВЛЕНО: было cl ose

                if stop_ev.is_set():
                    break

        with self._lock:
            self._thread = threading.Thread(target=_run, daemon=True)  # ИСПРАВЛЕНО: было daemo n
            self._thread.start()  # ИСПРАВЛЕНО: было s tart
            print(f'[CustomMusic] playback thread started')

# ---------------------------------------------------------------------------
# GUI: entry type select (used inside soundtrack editor)
# ---------------------------------------------------------------------------
class EntrySelectWindow(bui.MainWindow):
    """Choose between musicFile or default for a soundtrack slot."""

    def __init__(
        self,
        callback: Callable[[Any], None],
        current_entry: Any,
        selection_target_name: str,
        transition: str | None = 'in_right',
        origin_widget: bui.Widget | None = None,
    ) -> None:
        self._callback = callback
        self._current_entry = current_entry
        self._selection_target_name = selection_target_name

        width, height = 520, 310
        uiscale = bui.app.ui_v1.uiscale
        scale = (
            1.7 if uiscale is bui.UIScale.SMALL else
            1.4 if uiscale is bui.UIScale.MEDIUM else 1.0
        )
        super().__init__(
            root_widget=bui.containerwidget(size=(width, height), scale=scale),
            cleanupcheck=False,
            transition=transition,
            origin_widget=origin_widget,
        )

        btn = bui.buttonwidget(
            parent=self._root_widget,
            position=(30, height - 65), size=(130, 55), scale=0.8,
            label=bui.Lstr(resource='cancelText'),
            on_activate_call=self._cancel,  # ИСПРАВЛЕНО: было on_activate_call =self._cancel
        )
        bui.containerwidget(edit=self._root_widget, cancel_button=btn)

        bui.textwidget(
            parent=self._root_widget,
            position=(width * 0.5, height - 30), size=(0, 0),
            text='Источник музыки' if bui.app.lang.language == 'Russian' else 'Select Music Source',
            color=bui.app.ui_v1.title_color,
            maxwidth=300, h_align='center', v_align='center', scale=1.0,
        )
        bui.textwidget(
            parent=self._root_widget,
            position=(width * 0.5, height - 55), size=(0, 0),
            text=selection_target_name,
            color=bui.app.ui_v1.infotextcolor,  # ИСПРАВЛЕНО: было infotextcol or
            scale=0.7, maxwidth=300, h_align='center', v_align='center',
        )

        btn_w = width - 80
        v = height - 130

        bui.buttonwidget(
            parent=self._root_widget, position=(40, v), size=(btn_w, 52),
            label='Стандартная музыка игры' if bui.app.lang.language == 'Russian' else 'Use Default Game Music',
            on_activate_call=self._pick_default,
        )
        v -= 65
        bui.buttonwidget(
            parent=self._root_widget, position=(40, v), size=(btn_w, 52),
            label='Выбрать файл  (.wav)' if bui.app.lang.language == 'Russian' else 'Choose Music File  (.wav)',
            on_activate_call=self._pick_file,
        )



    def _cancel(self) -> None:
        self.main_window_back()

    def _pick_default(self) -> None:
        self._callback(None)
        self.main_window_back()

    def _pick_file(self) -> None:
        # ИСПРАВЛЕНИЕ: Убрал threading.Thread - нативный диалог должен вызываться из главного потока
        # Иначе он конфликтует с C++ движком BombSquad и ломает ассеты
        path = _pick_file_windows('Select Music File')
        if path:
            entry = {'type': 'musicFile', 'name': path}
            self._callback(entry)
        self.main_window_back()

    def main_window_should_preserve_selection(self) -> bool:
        return True

    def get_main_window_state(self) -> bui.MainWindowState:
        cls = type(self)
        callback = self._callback
        current_entry = self._current_entry
        selection_target_name = self._selection_target_name
        return bui.BasicMainWindowState(
            create_call=lambda transition, origin_widget: cls(
                callback=callback,
                current_entry=current_entry,
                selection_target_name=selection_target_name,
                transition=transition,
                origin_widget=origin_widget,
            )
        )

# ---------------------------------------------------------------------------
# GUI: main music manager window (opened from main menu button)
# ---------------------------------------------------------------------------
# Human-readable names for each music type
# Music type display names — (key: MusicType.value, value: display name)
_MUSIC_TYPE_NAMES_EN: dict[str, str] = {
    'Menu':         'Main Menu',
    'CharSelect':   'Character Selection',
    'Scores':       'Score Screen',
    'Victory':      'Final Score Screen',
    'Onslaught':    'Onslaught',
    'Keep Away':    'Keep Away',
    'Race':         'Race',
    'Epic Race':    'Epic Race',
    'ToTheDeath':   'Death Match',
    'Chosen One':   'Chosen One',
    'ForwardMarch': 'Assault',
    'FlagCatcher':  'Capture the Flag',
    'Survival':     'Elimination',
    'GrandRomp':    'Conquest',
    'Hockey':       'Hockey',
    'Football':     'Football',
    'Flying':       'Happy Thoughts',
    'Scary':        'King of the Hill',
    'Marching':     'Runaround',
    'Epic':         'Epic Mode Games',
    # Unused
    'RunAway':      'Run Away  (Unused)',
    'Sports':       'Sports  (Unused)',
}

_MUSIC_TYPE_NAMES_RU: dict[str, str] = {
    'Menu':         'Главное меню',
    'CharSelect':   'Выбор персонажа',
    'Scores':       'Счётное табло',
    'Victory':      'Табло финального счёта',
    'Onslaught':    'Атака',
    'Keep Away':    'Не подходить!',
    'Race':         'Гонка',
    'Epic Race':    'Эпическая гонка',
    'ToTheDeath':   'Смертельный бой',
    'Chosen One':   'Избранный',
    'ForwardMarch': 'Нападение',
    'FlagCatcher':  'Захват флага',
    'Survival':     'Ликвидация',
    'GrandRomp':    'Завоевание',
    'Hockey':       'Хоккей',
    'Football':     'Рэгби',
    'Flying':       'Счастливые мысли',
    'Scary':        'Царь горы',
    'Marching':     'Манёвр',
    'Epic':         'Игры в замедленном режиме',
    # Не используются ни одним режимом
    'RunAway':      'Побег  (не используется)',
    'Sports':       'Спорт  (не используется)',
}


def _get_music_type_names() -> dict[str, str]:
    """Return display names in the current game language."""
    try:
        lang = bui.app.lang.language
        if lang == 'Russian':
            return _MUSIC_TYPE_NAMES_RU
    except Exception:
        pass
    return _MUSIC_TYPE_NAMES_EN


# Alias used throughout the UI — resolves at call time
def _music_type_names() -> dict[str, str]:
    return _get_music_type_names()

class MusicManagerWindow(bui.MainWindow):
    """Main window for managing custom music per soundtrack slot."""

    def __init__(
        self,
        transition: str | None = 'in_right',
        origin_widget: bui.Widget | None = None,
    ) -> None:
        uiscale = bui.app.ui_v1.uiscale
        self._width = 650
        self._height = 500
        scale = (
            1.5 if uiscale is bui.UIScale.SMALL else
            1.2 if uiscale is bui.UIScale.MEDIUM else 1.0
        )
        super().__init__(
            root_widget=bui.containerwidget(
                size=(self._width, self._height), scale=scale,
            ),
            transition=transition,
            origin_widget=origin_widget,
        )
        self._build_ui()

    def _build_ui(self) -> None:
        w, h = self._width, self._height

        # Back button
        back_btn = bui.buttonwidget(
            parent=self._root_widget,
            position=(50, h - 65), size=(130, 55), scale=0.8,
            label=bui.Lstr(resource='backText'),
            button_type='back',
            on_activate_call=self.main_window_back,
            autoselect=True,
        )
        bui.containerwidget(edit=self._root_widget, cancel_button=back_btn)

        # Title
        bui.textwidget(
            parent=self._root_widget,
            position=(w * 0.5, h - 38), size=(0, 0),
            text='Своя музыка' if bui.app.lang.language == 'Russian' else 'Custom Music', scale=1.2,
            color=bui.app.ui_v1.title_color,
            h_align='center', v_align='center', maxwidth=300,
        )

        # Subtitle
        bui.textwidget(
            parent=self._root_widget,
            position=(w * 0.5, h - 62), size=(0, 0),
            text='Нажми слот чтобы назначить файл' if bui.app.lang.language == 'Russian' else 'Click a slot to assign a custom file',
            scale=0.6, color=(0.7, 0.7, 0.7),
            h_align='center', v_align='center', maxwidth=400,
        )

        # Scrollable list of soundtrack slots
        scroll_h = h - 130
        self._scrollwidget = bui.scrollwidget(
            parent=self._root_widget,
            position=(30, 55),
            size=(w - 60, scroll_h),
        )
        self._col = bui.columnwidget(
            parent=self._scrollwidget,
            border=2, margin=0,
        )

        self._refresh_list()

        # Reset all button
        bui.buttonwidget(
            parent=self._root_widget,
            position=(w * 0.5 - 100, 10), size=(200, 38),
            label='Сбросить всё' if bui.app.lang.language == 'Russian' else 'Reset All to Default',
            color=(0.6, 0.3, 0.3),
            on_activate_call=self._reset_all,
            autoselect=True,
        )

    def _refresh_list(self) -> None:
        """Rebuild the list of music type rows."""
        # Clear existing children
        for child in self._col.get_children():
            child.delete()

        cfg = bui.app.config
        soundtrack_name = cfg.get('Soundtrack', '__default__')
        soundtrack: dict[str, Any] = {}
        if soundtrack_name not in ('__default__', 'Default Soundtrack'):
            try:
                soundtrack = cfg.get('Soundtracks', {}).get(soundtrack_name, {})
            except Exception:
                pass

        row_w = self._width - 80

        for music_type_val, display_name in _music_type_names().items():
            entry = soundtrack.get(music_type_val)
            self._make_row(music_type_val, display_name, entry, row_w)

    def _make_row(
        self,
        music_type_val: str,
        display_name: str,
        entry: Any,
        row_w: float,
    ) -> None:
        row = bui.containerwidget(
            parent=self._col,
            size=(row_w, 48),
            background=False,
        )

        # Music type label
        bui.textwidget(
            parent=row,
            position=(8, 24), size=(0, 0),
            text=display_name,
            scale=0.7, maxwidth=220,
            h_align='left', v_align='center',
            color=(0.85, 0.85, 0.85),
        )

        # Current assignment label
        assign_text = self._entry_label(entry)
        bui.buttonwidget(
            parent=row,
            position=(240, 6), size=(row_w - 250, 36),
            label=assign_text,
            text_scale=0.6,
            color=(0.35, 0.45, 0.35) if entry else (0.3, 0.3, 0.3),
            on_activate_call=babase.CallPartial(
                self._edit_slot, music_type_val, display_name, entry
            ),
            autoselect=True,
        )

    def _entry_label(self, entry: Any) -> str:
        if entry is None:
            return 'Default'
        music = babase.app.classic.music
        etype = music.get_soundtrack_entry_type(entry)
        name = music.get_soundtrack_entry_name(entry)
        if etype == 'musicFile':
            return '🎵 ' + os.path.basename(name)
        return 'Default'

    def _edit_slot(
        self, music_type_val: str, display_name: str, entry: Any
    ) -> None:
        if not self.main_window_has_control():
            return

        def _callback(new_entry: Any) -> None:
            self._apply_entry(music_type_val, new_entry)

        win = EntrySelectWindow(
            callback=_callback,
            current_entry=entry,
            selection_target_name=display_name,
        )
        self.main_window_replace(lambda: win)

    def _apply_entry(self, music_type_val: str, entry: Any) -> None:
        """Save the chosen entry into the active soundtrack config."""
        cfg = bui.app.config

        # Make sure we have a custom soundtrack active; create one if needed
        soundtrack_name = cfg.get('Soundtrack', '__default__')
        if soundtrack_name in ('__default__', 'Default Soundtrack', None):
            soundtrack_name = 'Custom'
            cfg['Soundtrack'] = soundtrack_name

        if 'Soundtracks' not in cfg:
            cfg['Soundtracks'] = {}
        if soundtrack_name not in cfg['Soundtracks']:
            cfg['Soundtracks'][soundtrack_name] = {}

        if entry is None:
            cfg['Soundtracks'][soundtrack_name].pop(music_type_val, None)
        else:
            cfg['Soundtracks'][soundtrack_name][music_type_val] = entry

        cfg.commit()

        
        music = babase.app.classic.music
        if hasattr(music, 'set_soundtrack'):
            music.set_soundtrack(cfg.get('Soundtrack', '__default__'))
        elif hasattr(music, '_reload'):
            music._reload()
        elif hasattr(music, 'apply_config'):
            music.apply_config()

        try:
            music.set_music_play_mode(
                babase.app.classic.MusicPlayMode.REGULAR, force_restart=True
            )
        except Exception as e:
            print(f'[CustomMusic] Music restart failed: {e}')

        bui.screenmessage('Сохранено!' if bui.app.lang.language == 'Russian' else 'Saved!', color=(0.5, 1.0, 0.5))

    def _reset_all(self) -> None:
        cfg = bui.app.config
        soundtrack_name = cfg.get('Soundtrack', '__default__')
        if soundtrack_name not in ('__default__', 'Default Soundtrack'):
            try:
                cfg.get('Soundtracks', {}).pop(soundtrack_name, None)
            except Exception:
                pass
            cfg['Soundtrack'] = '__default__'
            cfg.commit()
        
        music = babase.app.classic.music
        if hasattr(music, 'set_soundtrack'):
            music.set_soundtrack(cfg.get('Soundtrack', '__default__'))
        elif hasattr(music, '_reload'):
            music._reload()
        elif hasattr(music, 'apply_config'):
            music.apply_config()
        
        try:
            music.set_music_play_mode(
                babase.app.classic.MusicPlayMode.REGULAR, force_restart=True
            )
        except Exception as e:
            print(f'[CustomMusic] Music restart failed: {e}')
        
        bui.screenmessage('Всё сброшено до стандартного.' if bui.app.lang.language == 'Russian' else 'All reset to default.', color=(1.0, 0.8, 0.4))
        self._refresh_list()

    def main_window_should_preserve_selection(self) -> bool:
        return True

    def get_main_window_state(self) -> bui.MainWindowState:
        cls = type(self)
        return bui.BasicMainWindowState(
            create_call=lambda transition, origin_widget: cls(
                transition=transition, origin_widget=origin_widget,
            )
        )

# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------
# ba_meta export babase.Plugin
class CustomMusicWindowsPlugin(babase.Plugin):
    def on_app_loading(self) -> None:
        self._install_player()
        self._inject_menu_button()

    def _install_player(self) -> None:
        # supports_soundtrack_entry_type is already patched at class level
        # (module-load time). Here we just point the music system at our player.
        try:
            music = babase.app.classic.music
            music._music_player_type = WindowsMusicPlayer
            music._music_player = None
            print('[CustomMusic] Player installed.')
        except Exception as e:
            print(f'[CustomMusic] Failed to install player: {e}')

    def _inject_menu_button(self) -> None:
        pass  # injection happens at module level below

def _open_music_manager() -> None:
    win = bui.app.ui_v1.get_main_window()
    if win is not None:
        win.main_window_replace(lambda: MusicManagerWindow(transition='in_right'))

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
def _inject_music_button() -> None:
    try:
        import bauiv1lib.mainmenu as mm
        if getattr(mm.MainMenuWindow.__init__, '_custom_music_patched', False):
            return
        old_init = mm.MainMenuWindow.__init__

        def new_init(self: Any, *args: Any, **kwargs: Any) -> None:
            old_init(self, *args, **kwargs)

            # Mirror the layout math from MainMenuWindow._refresh()
            # so our button sits naturally alongside the existing buttons.
            uiscale = bui.app.ui_v1.uiscale
            button_width   = 200.0
            button_height  = 45.0
            play_bw        = button_width * 0.65
            play_bscale    = 1.7
            hspace2        = 15.0
            s2w            = button_width * 1.0          # side_button_2_width
            s2h            = s2w * 0.3                   # side_button_2_height
            s2scale        = 0.5                         # side_button_2_scale
            sw             = button_width * 0.4          # side_button_width
            sw_scale       = 0.95
            hspace         = 20.0
            width          = 400.0

            if uiscale is bui.UIScale.SMALL:
                button_y_offs = -20.0
                button_height *= 1.3
            elif uiscale is bui.UIScale.MEDIUM:
                button_y_offs = -55.0
                button_height *= 1.25
            else:
                button_y_offs = -90.0
                button_height *= 1.2

            s2_y_offs = 10.0

            # h position: same column as Credits/Quit (rightmost column)
            h = (
                width * 0.5
                + play_bw * play_bscale * 0.5
                + hspace
                + sw * sw_scale * 0.5
                + sw * sw_scale * 0.5
                + hspace2
            )
            # Place our button just below the Quit button
            # (quit sits at button_y_offs + s2_y_offs, our button goes below)
            v = button_y_offs + s2_y_offs - 1.17 * s2h * s2scale - 1.1 * s2h * s2scale

            label = ('🎵 Музыка' if bui.app.lang.language == 'Russian'
                     else '🎵 Music')
            bui.buttonwidget(
                parent=self._root_widget,
                position=(h, v),
                size=(s2w, s2h),
                scale=s2scale,
                label=label,
                button_type='square',
                autoselect=True,
                id='custom_music_btn',
                on_activate_call=_open_music_manager,
            )

        new_init._custom_music_patched = True
        mm.MainMenuWindow.__init__ = new_init
    except Exception as e:
        print(f'[CustomMusic] inject error: {e}')

def _install_player_now() -> None:.
    try:
        music = babase.app.classic.music
        music._music_player_type = WindowsMusicPlayer
        music._music_player = None
        print('[CustomMusic] Player installed (module level).')

        # on_app_loading already ran and possibly stopped at get_music_player()
        # failure. Re-trigger playback for the current music type.
        current_type = music.music_types.get(
            babase.app.classic.MusicPlayMode.REGULAR
        )
        if current_type is not None:
            music.do_play_music(current_type.value)
            print(f'[CustomMusic] Triggered playback for {current_type.value}')
        else:
            # Music type not set yet — schedule a retry via pushcall so the
            # app finishes loading first, then we replay Menu music.
            def _deferred_start() -> None:
                try:
                    music2 = babase.app.classic.music
                    ct = music2.music_types.get(babase.app.classic.MusicPlayMode.REGULAR)
                    if ct is not None:
                        music2.do_play_music(ct.value)
                        print(f'[CustomMusic] Deferred playback: {ct.value}')
                except Exception as ex:
                    print(f'[CustomMusic] Deferred playback error: {ex}')
            babase.pushcall(_deferred_start)
    except Exception as e:
        print(f'[CustomMusic] Failed to install player: {e}')

_inject_music_button()
_install_player_now()