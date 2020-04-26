from collections import defaultdict

from kivy.clock import Clock
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label

from common import OUTPUT_DEBUG, OUTPUT_ERROR
from engine import KataGoEngine
from game import Game, GameNode
from gui.kivyutils import (
    LabelledCheckBox,
    LabelledFloatInput,
    LabelledIntInput,
    LabelledObjectInputArea,
    LabelledSpinner,
    LabelledTextInput,
    ScaledLightLabel,
    StyledButton,
    LightHelpLabel,
)


class InputParseError(Exception):
    pass


class QuickConfigGui(BoxLayout):
    def __init__(self, katrain, popup, initial_values=None, **kwargs):
        super().__init__(**kwargs)
        self.katrain = katrain
        self.popup = popup
        if initial_values:
            self.set_properties(self, initial_values)

    @staticmethod
    def type_to_widget_class(value):
        if isinstance(value, float):
            return LabelledFloatInput
        elif isinstance(value, bool):
            return LabelledCheckBox
        elif isinstance(value, int):
            return LabelledIntInput
        if isinstance(value, dict):
            return LabelledObjectInputArea
        else:
            return LabelledTextInput

    def collect_properties(self, widget):
        if isinstance(widget, (LabelledTextInput, LabelledSpinner, LabelledCheckBox)):
            try:
                ret = {widget.input_property: widget.input_value}
            except Exception as e:
                raise InputParseError(f"Could not parse value for {widget.input_property} ({widget.__class__}): {e}")
        else:
            ret = {}
        for c in widget.children:
            for k, v in self.collect_properties(c).items():
                ret[k] = v
        return ret

    def set_properties(self, widget, properties):
        if isinstance(widget, (LabelledTextInput, LabelledSpinner)):
            key = widget.input_property
            if key in properties:
                widget.text = str(properties[key])
        for c in widget.children:
            self.set_properties(c, properties)


class LoadSGFPopup(BoxLayout):
    pass


class NewGamePopup(QuickConfigGui):
    def __init__(self, katrain, popup, properties, **kwargs):
        properties["RU"] = KataGoEngine.get_rules(katrain.game.root)
        super().__init__(katrain, popup, properties, **kwargs)
        self.rules_spinner.values = list(set(self.katrain.engine.RULESETS.values()))
        self.rules_spinner.text = properties["RU"]

    def new_game(self):
        properties = self.collect_properties(self)
        self.katrain.log(f"New game settings: {properties}", OUTPUT_DEBUG)
        new_root = GameNode(properties={**Game.DEFAULT_PROPERTIES, **properties})
        x, y = new_root.board_size
        if x > 52 or y > 52:
            self.info.text = "Board size too big, should be at most 52"
            return
        self.katrain("new-game", new_root)
        self.popup.dismiss()


class ConfigPopup(QuickConfigGui):
    def __init__(self, katrain, popup, config, ignore_cats, **kwargs):
        self.config = config
        self.ignore_cats = ignore_cats
        self.orientation = "vertical"
        super().__init__(katrain, popup, **kwargs)
        Clock.schedule_once(self._build, 0)

    def _build(self, _):
        cols = [BoxLayout(orientation="vertical"), BoxLayout(orientation="vertical")]
        props_in_col = [0, 0]
        for k1, all_d in sorted(self.config.items(), key=lambda tup: -len(tup[1])):  # sort to make greedy bin packing work better
            if k1 in self.ignore_cats:
                continue
            d = {k: v for k, v in all_d.items() if isinstance(v, (int, float, str, bool))}  # no lists . dict could be supported but hard to scale
            cat = GridLayout(cols=2, rows=len(d) + 1, size_hint=(1, len(d) + 1))
            cat.add_widget(Label(text=""))
            cat.add_widget(ScaledLightLabel(text=f"{k1} settings", bold=True))
            for k2, v in d.items():
                cat.add_widget(ScaledLightLabel(text=f"{k2}:"))
                cat.add_widget(self.type_to_widget_class(v)(text=str(v), input_property=f"{k1}/{k2}"))
            if props_in_col[0] <= props_in_col[1]:
                cols[0].add_widget(cat)
                props_in_col[0] += len(d)
            else:
                cols[1].add_widget(cat)
                props_in_col[1] += len(d)

        col_container = BoxLayout(size_hint=(1, 0.9))
        col_container.add_widget(cols[0])
        col_container.add_widget(cols[1])
        self.add_widget(col_container)
        self.info_label = Label()
        self.apply_button = StyledButton(text="Apply", on_press=lambda _: self.update_config())
        self.save_button = StyledButton(text="Apply and Save", on_press=lambda _: self.update_config(save_to_file=True))
        btn_container = BoxLayout(orientation="horizontal", size_hint=(1, 0.1), spacing=1, padding=1)
        btn_container.add_widget(self.info_label)
        btn_container.add_widget(self.apply_button)
        btn_container.add_widget(self.save_button)
        self.add_widget(btn_container)

    def update_config(self, save_to_file=False):
        updated_cat = defaultdict(list)
        try:
            for k, v in self.collect_properties(self).items():
                k1, k2 = k.split("/")
                if self.config[k1][k2] != v:
                    self.katrain.log(f"Updating setting {k} = {v}", OUTPUT_DEBUG)
                    updated_cat[k1].append(k2)
                    self.config[k1][k2] = v
            self.popup.dismiss()
        except InputParseError as e:
            self.info_label.text = str(e)
            self.katrain.log(e, OUTPUT_ERROR)
            return

        if save_to_file:
            self.katrain.save_config()

        engine_updates = updated_cat["engine"]
        if "visits" in engine_updates:
            self.katrain.engine.visits = engine_updates["visits"]
        if {key for key in engine_updates if key not in {"max_visits", "max_time", "enable_ownership"}}:
            self.katrain.log(f"Restarting Engine after {engine_updates} settings change")
            self.katrain.controls.set_status(f"Restarting Engine after {engine_updates} settings change")
            old_engine = self.katrain.engine
            new_engine = KataGoEngine(self.katrain, self.config["engine"])
            self.katrain.engine = {"B": new_engine, "W": new_engine}
            self.katrain.game.engine = new_engine
            if getattr(old_engine, "katago_process"):
                old_engine.shutdown(finish=True)
            else:
                self.katrain.game.analyze_all_nodes()  # old engine was broken, so make sure we redo any failures

        self.katrain.debug_level = self.config["debug"]["level"]
        self.katrain.update_state(redraw_board=True)


class ConfigAIPopup(QuickConfigGui):
    def __init__(self, katrain, popup, ai_modes, **kwargs):
        super().__init__(katrain, popup, katrain.ai_settings, **kwargs)
        self.settings = self.katrain.ai_settings
        self.ai_modes = ai_modes
        Clock.schedule_once(self._build, 0)
        self.orientation = "vertical"

    def _build(self, _dt):
        colbox = BoxLayout(spacing=5)
        for mode in self.ai_modes:
            mode_settings = self.settings[mode]
            num_rows = len(mode_settings) - 2
            column = GridLayout(cols=2, rows=max(num_rows, 4) + 3, spacing=1, padding=3)
            column.add_widget(ScaledLightLabel(text=f"Settings for AI"))
            column.add_widget(ScaledLightLabel(text=f"{mode}", bold=True))
            column.add_widget(LightHelpLabel(size_hint=(1, 3), text=mode_settings.get("_help_left", "")))
            column.add_widget(LightHelpLabel(size_hint=(1, 3), text=mode_settings.get("_help_right", "")))
            for k, v in mode_settings.items():
                if not k.startswith("_"):
                    column.add_widget(ScaledLightLabel(text=f"{k}"))
                    column.add_widget(ConfigPopup.type_to_widget_class(v)(text=str(v), input_property=f"{mode}/{k}"))
            for _ in range(4 - num_rows):
                column.add_widget(ScaledLightLabel(text=f""))
                column.add_widget(ScaledLightLabel(text=f""))
            colbox.add_widget(column)
        if len(self.ai_modes) == 1:
            colbox.add_widget(ScaledLightLabel(text=f""))
        bl = BoxLayout(size_hint=(1, 0.2), spacing=2)
        bl.add_widget(StyledButton(text=f"Apply", on_press=lambda _: self.update_config(False)))
        bl.add_widget(StyledButton(text=f"Apply and Save", on_press=lambda _: self.update_config(True)))
        self.add_widget(colbox)
        self.add_widget(bl)

    def update_config(self, save_to_file=False):
        try:
            for k, v in self.collect_properties(self).items():
                k1, k2 = k.split("/")
                print(k1, k2, v, self.settings[k1][k2])
                if self.settings[k1][k2] != v:
                    self.katrain.log(f"Updating setting {k} = {v}", OUTPUT_DEBUG)

            self.popup.dismiss()
        except InputParseError as e:
            self.info_label.text = str(e)
            self.katrain.log(e, OUTPUT_ERROR)
            return

        self.popup.dismiss()
