from kivy.config import Config  # isort:skip

ICON = "img/icon.png"  # isort:skip  # set icon
Config.set("kivy", "window_icon", ICON)  # isort:skip  # set icon
Config.set("input", "mouse", "mouse,multitouch_on_demand")  # isort:skip  # no red dots on right click
Config.set('graphics', 'width', 1400)
Config.set('graphics', 'height', 1000)

import os
import signal
import sys
import threading
import traceback
from queue import Queue

from kivy.app import App
from kivy.core.clipboard import Clipboard
from kivy.lang import Builder
from kivy.properties import ObjectProperty
from kivy.resources import resource_add_path
from kivy.storage.jsonstore import JsonStore
from kivy.uix.popup import Popup
from kivy.uix.screenmanager import Screen
from kivymd.app import MDApp

from katrain.core.ai import ai_move
from katrain.core.utils import LANGUAGE, OUTPUT_DEBUG, OUTPUT_ERROR, OUTPUT_EXTRA_DEBUG, OUTPUT_INFO, OUTPUT_KATAGO_STDERR, find_package_resource, i18n
from katrain.core.engine import KataGoEngine
from katrain.core.game import Game, IllegalMoveException, KaTrainSGF
from katrain.core.sgf_parser import Move, ParseError
from katrain.gui.badukpan import AnalysisControls, BadukPanControls, BadukPanWidget
from katrain.gui.controlspanel import ControlsPanel
from katrain.gui.graph import ScoreGraph
from katrain.gui.kivyutils import *
from katrain.gui.popups import ConfigPopup, LoadSGFPopup, NewGamePopup
from katrain.gui.style import ENGINE_BUSY_COL, ENGINE_DOWN_COL, ENGINE_READY_COL
from katrain.gui.style import *

__version__ = "1.1.0"


class KaTrainGui(Screen):
    """Top level class responsible for tying everything together"""

    zen = BooleanProperty(False)

    def __init__(self, **kwargs):
        super(KaTrainGui, self).__init__(**kwargs)
        self.debug_level = 0
        self.engine = None
        self.game = None
        self.new_game_popup = None
        self.fileselect_popup = None
        self.config_popup = None
        self.logger = lambda message, level=OUTPUT_INFO: self.log(message, level)
        self.config_file = self._load_config()

        self.debug_level = self.config("debug/level", OUTPUT_INFO)
        self.message_queue = Queue()

        self._keyboard = Window.request_keyboard(None, self, "")
        self._keyboard.bind(on_key_down=self._on_keyboard_down)

    def log(self, message, level=OUTPUT_INFO):
        if level == OUTPUT_KATAGO_STDERR and "ERROR" not in self.controls.status_label.text:
            if "starting" in message.lower():
                self.controls.set_status(f"KataGo engine starting...")
            if message.startswith("Tuning"):
                self.controls.set_status(f"KataGo is tuning settings for first startup, please wait." + message)
                return
            if "ready" in message.lower():
                self.controls.set_status(f"KataGo engine ready.")
            if "ready" in message.lower():
                self.controls.set_status(f"KataGo engine ready.")
        if level == OUTPUT_ERROR or (level == OUTPUT_KATAGO_STDERR and "error" in message.lower()):
            self.controls.set_status(f"ERROR: {message}")
            print(f"ERROR: {message}")
        elif self.debug_level >= level:
            print(message)

    def _load_config(self):
        config_file = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else find_package_resource("katrain/config.json"))
        try:
            self.log(f"Using config file {config_file}", OUTPUT_INFO)
            self._config_store = JsonStore(config_file, indent=4)
            self._config = dict(self._config_store)
            return config_file
        except Exception as e:
            self.log(f"Failed to load config {config_file}: {e}", OUTPUT_ERROR)
            sys.exit(1)

    def save_config(self):
        for k, v in self._config.items():
            self._config_store.put(k, **v)

    def config(self, setting, default=None):
        try:
            if "/" in setting:
                cat, key = setting.split("/")
                return self._config[cat].get(key, default)
            else:
                return self._config[setting]
        except KeyError:
            self.log(f"Missing configuration option {setting}", OUTPUT_ERROR)

    def start(self):
        if self.engine:
            return
        self.board_gui.trainer_config = self.config("trainer")
        self.engine = KataGoEngine(self, self.config("engine"))
        threading.Thread(target=self._message_loop_thread, daemon=True).start()
        self._do_new_game()

    def update_state(self, redraw_board=False):  # is called after every message and on receiving analyses and config changes
        # AI and Trainer/auto-undo handlers
        cn = self.game.current_node
        if self.controls.play_analyze_mode == "play":
            auto_undo = cn.player and "undo" in self.controls.player_mode(cn.player)
            if auto_undo and cn.analysis_ready and cn.parent and cn.parent.analysis_ready and not cn.children and not self.game.ended:
                self.game.analyze_undo(cn, self.config("trainer"))  # not via message loop
            if (
                cn.analysis_ready
                and "ai" in self.controls.player_mode(cn.next_player).lower()
                and not cn.children
                and not self.game.ended
                and not (auto_undo and cn.auto_undo is None)
            ):
                self._do_ai_move(cn)  # cn mismatch stops this if undo fired. avoid message loop here or fires repeatedly.

        # Handle prisoners and next player display
        prisoners = self.game.prisoner_count
        top, bot = [w.__self__ for w in self.board_controls.circles]  # no weakref
        if self.game.next_player.player == "W":
            top, bot = bot, top
            self.controls.players['W'].active = True
            self.controls.players['B'].active = False
        else:
            self.controls.players['W'].active = False
            self.controls.players['B'].active = True
        self.board_controls.mid_circles_container.clear_widgets()
        self.board_controls.mid_circles_container.add_widget(bot)
        self.board_controls.mid_circles_container.add_widget(top)
        self.controls.players["W"].captures = prisoners["W"]
        self.controls.players["B"].captures = prisoners["B"]

        # update engine status dot
        if not self.engine or not self.engine.katago_process or self.engine.katago_process.poll() is not None:
            self.board_controls.engine_status_col = ENGINE_DOWN_COL
        elif len(self.engine.queries) == 0:
            self.board_controls.engine_status_col = ENGINE_READY_COL
        else:
            self.board_controls.engine_status_col = ENGINE_BUSY_COL

        # redraw
        if redraw_board:
            Clock.schedule_once(self.board_gui.draw_board, -1)
        self.board_gui.redraw_board_contents_trigger()
        self.controls.update_evaluation()
        self.controls.update_timer(1)

    def set_note(self, note):
        self.game.current_node.note = note

    def _message_loop_thread(self):
        while True:
            game, msg, *args = self.message_queue.get()
            try:
                self.log(f"Message Loop Received {msg}: {args} for Game {game}", OUTPUT_EXTRA_DEBUG)
                if game != self.game.game_id:
                    self.log(f"Message skipped as it is outdated (current game is {self.game.game_id}", OUTPUT_EXTRA_DEBUG)
                    continue
                getattr(self, f"_do_{msg.replace('-','_')}")(*args)
                self.update_state()
            except Exception as e:
                self.log(f"Exception in processing message {msg} {args}: {e}", OUTPUT_ERROR)
                traceback.print_exc()

    def __call__(self, message, *args):
        if self.game:
            self.message_queue.put([self.game.game_id, message, *args])

    def update_players(self):
        for player in 'BW':
            self.controls.players[player].player_type = self.game.players[player].player_type
            self.game.players[player].player_subtype =  self.game.players[player].player_subtype

    def _do_new_game(self, move_tree=None, analyze_fast=False):
        self.board_gui.animating_pv = None
        self.engine.on_new_game()  # clear queries
        self.game = Game(self, self.engine, self.config("game"), move_tree=move_tree, analyze_fast=analyze_fast)
        self.controls.graph.initialize_from_game(self.game.root)
        self.controls.periods_used = {"B": 0, "W": 0}
        self.update_state(redraw_board=True)
        self.update_players()

    def _do_ai_move(self, node=None):
        if node is None or self.game.current_node == node:
            mode = self.controls.ai_mode(self.game.current_node.next_player)
            settings = self.config(f"ai/{mode}")
            if settings:
                ai_move(self.game, mode, settings)

    def _do_undo(self, n_times=1):
        self.board_gui.animating_pv = None
        self.game.undo(n_times)

    def _do_redo(self, n_times=1):
        self.board_gui.animating_pv = None
        self.game.redo(n_times)

    def _do_switch_branch(self, direction):
        self.board_gui.animating_pv = None
        self.game.switch_branch(direction)

    def _do_play(self, coords):
        self.board_gui.animating_pv = None
        try:
            self.game.play(Move(coords, player=self.game.next_player.player))
        except IllegalMoveException as e:
            self.controls.set_status(f"Illegal Move: {str(e)}")

    def _do_analyze_extra(self, mode):
        self.game.analyze_extra(mode)

    def _do_analyze_sgf_popup(self):
        if not self.fileselect_popup:
            self.fileselect_popup = Popup(title="Double Click SGF file to analyze", size_hint=(0.8, 0.8)).__self__
            popup_contents = LoadSGFPopup()
            self.fileselect_popup.add_widget(popup_contents)
            popup_contents.filesel.path = os.path.abspath(os.path.expanduser(self.config("sgf/sgf_load")))

            def readfile(files, _mouse):
                self.fileselect_popup.dismiss()
                try:
                    move_tree = KaTrainSGF.parse_file(files[0])
                except ParseError as e:
                    self.log(i18n._("Failed to load SGF").format(error=e), OUTPUT_ERROR)
                    return
                self._do_new_game(move_tree=move_tree, analyze_fast=popup_contents.fast.active)
                if not popup_contents.rewind.active:
                    self.game.redo(999)

            popup_contents.filesel.on_submit = readfile
        self.fileselect_popup.open()

    def _do_new_game_popup(self):
        if not self.new_game_popup:
            self.new_game_popup = Popup(title="New Game", size_hint=(0.5, 0.6)).__self__
            popup_contents = NewGamePopup(self, self.new_game_popup, {k: v[0] for k, v in self.game.root.properties.items() if len(v) == 1})
            self.new_game_popup.add_widget(popup_contents)
        self.new_game_popup.open()

    def _do_config_popup(self):
        if not self.config_popup:
            self.config_popup = Popup(title=f"Edit Settings - {self.config_file}", size_hint=(0.9, 0.9)).__self__
            popup_contents = ConfigPopup(self, self.config_popup, dict(self._config), ignore_cats=("trainer", "ai"))
            self.config_popup.add_widget(popup_contents)
        self.config_popup.open()

    def _do_output_sgf(self):
        for pl in Move.PLAYERS:
            if not self.game.root.get_property(f"P{pl}"):
                _, model_file = os.path.split(self.engine.config["model"])
                self.game.root.set_property(
                    f"P{pl}", f"AI {self.controls.ai_mode(pl)} (KataGo { os.path.splitext(model_file)[0]})" if "ai" in self.controls.player_mode(pl) else "Player"
                )
        msg = self.game.write_sgf(
            self.config("sgf/sgf_save"),
            trainer_config=self.config("trainer"),
            save_feedback=self.config("sgf/save_feedback"),
            eval_thresholds=self.config("trainer/eval_thresholds"),
        )
        self.log(msg, OUTPUT_INFO)
        self.controls.set_status(msg)

    def load_sgf_from_clipboard(self):
        clipboard = Clipboard.paste()
        if not clipboard:
            self.controls.set_status(f"Ctrl-V pressed but clipboard is empty.")
            return
        try:
            move_tree = KaTrainSGF.parse(clipboard)
        except Exception as e:
            self.controls.set_status(i18n._("Failed to import from clipboard").format(error=e, contents=clipboard[:50]))
            return
        move_tree.nodes_in_tree[-1].analyze(self.engine, analyze_fast=False)  # speed up result for looking at end of game
        self._do_new_game(move_tree=move_tree, analyze_fast=True)
        self("redo", 999)
        self.log("Imported game from clipboard.", OUTPUT_INFO)

    def on_touch_up(self, touch):
        if self.board_gui.collide_point(*touch.pos) or self.board_controls.collide_point(*touch.pos):
            if touch.button == "scrollup":
                self("redo")
            elif touch.button == "scrolldown":
                self("undo")
        return super().on_touch_up(touch)

    def _on_keyboard_down(self, _keyboard, keycode, _text, modifiers):
        if isinstance(App.get_running_app().root_window.children[0], Popup) or self.controls.note.focus:
            return  # if in new game or load, don't allow keyboard shortcuts

        shortcuts = {
            "w": self.analysis_controls.eval,
            "e": self.analysis_controls.hints,
            "r": self.analysis_controls.ownership,
            "t": self.analysis_controls.policy,
            "enter": ("ai-move",),
            "a": ("analyze-extra", "extra"),
            "s": ("analyze-extra", "equalize"),
            "d": ("analyze-extra", "sweep"),
            "p": ("play", None),
            "right": ("switch-branch", 1),
            "left": ("switch-branch", -1),
        }
        if keycode[1] in shortcuts.keys():
            shortcut = shortcuts[keycode[1]]
            if isinstance(shortcut, Widget):
                shortcut.trigger_action(duration=0)
            else:
                self(*shortcut)
        elif keycode[1] == "tab":
            self.controls.switch_mode()
        elif keycode[1] == "spacebar":
            self.controls.timer.paused = not self.controls.timer.paused
        elif keycode[1] in ["`", "~", "m"]:
            self.zen = not self.zen
        elif keycode[1] in ["up", "z"]:
            self("undo", 1 + ("shift" in modifiers) * 9 + ("ctrl" in modifiers) * 999)
        elif keycode[1] in ["down", "x"]:
            self("redo", 1 + ("shift" in modifiers) * 9 + ("ctrl" in modifiers) * 999)
        elif keycode[1] == "n" and "ctrl" in modifiers:
            self("new-game-popup")
        elif keycode[1] == "l" and "ctrl" in modifiers:
            self("analyze-sgf-popup")
        elif keycode[1] == "s" and "ctrl" in modifiers:
            self("output-sgf")
        elif keycode[1] == "c" and "ctrl" in modifiers:
            Clipboard.copy(self.game.root.sgf())
            self.controls.set_status(i18n._("Copied SGF to clipboard."))
        elif keycode[1] == "v" and "ctrl" in modifiers:
            self.load_sgf_from_clipboard()
        return True


class KaTrainApp(MDApp):
    gui = ObjectProperty(None)
    language = StringProperty(LANGUAGE)

    def build(self):
        self.icon = ICON  # how you're supposed to set an icon
        self.gui = KaTrainGui()
        self.title = f"KaTrain v{__version__}"
        self.theme_cls.theme_style = "Dark"
        self.theme_cls.primary_palette = "Blue"

        Window.bind(on_request_close=self.on_request_close)
        return self.gui

    def on_language(self, _instance, language):
        self.gui.log(f"Switching language to {language}", OUTPUT_INFO)
        i18n.switch_lang(language)

    def on_start(self):
        self.gui.start()

    def on_request_close(self, *_args):
        if getattr(self, "gui", None) and self.gui.engine:
            self.gui.engine.shutdown()

    def signal_handler(self, _signal, _frame):
        if self.gui.debug_level >= OUTPUT_DEBUG:
            print("TRACEBACKS")
            for threadId, stack in sys._current_frames().items():
                print(f"\n# ThreadID: {threadId}")
                for filename, lineno, name, line in traceback.extract_stack(stack):
                    print(f"\tFile: {filename}, line {lineno}, in {name}")
                    if line:
                        print(f"\t\t{line.strip()}")
        self.on_request_close()
        sys.exit(0)


def run_app():
    kv_file = find_package_resource("katrain/gui.kv")
    resource_add_path(os.path.split(kv_file)[0])
    Builder.load_file(kv_file)
    app = KaTrainApp()
    signal.signal(signal.SIGINT, app.signal_handler)
    try:
        app.run()
    except Exception:
        app.on_request_close()
        raise


if __name__ == "__main__":
    run_app()
