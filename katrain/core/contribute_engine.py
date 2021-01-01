import json
import os
import random
import shlex
import subprocess
import threading
import time
import traceback


from katrain.core.constants import OUTPUT_DEBUG, OUTPUT_ERROR, OUTPUT_KATAGO_STDERR, OUTPUT_INFO
from katrain.core.engine import EngineDiedException
from katrain.core.game import BaseGame
from katrain.core.lang import i18n
from katrain.core.sgf_parser import Move


class KataGoContributeEngine:
    """Starts and communicates with the KataGO contribute program"""

    MAX_BUFFER_GAMES = 8
    MOVE_SPEED = 0.5
    SHOW_RESULT_TIME = 5
    GIVE_UP_AFTER = 30

    def __init__(self, katrain):
        self.katrain = katrain
        cfg = os.path.expanduser("~/.katrain/contribute.cfg")
        base_dir = os.path.expanduser("~/.katrain/katago_contribute")
        self.stdout_thread = None
        self.stderr_thread = None
        self.shell = False
        self.active_games = {}
        self.finished_games = set()
        self.showing_game = None
        self.last_advance = 0

        self.save_sgf = True

        exe = os.path.expanduser("~/.katrain/katago.exe")

        settings_dict = {'username':'sander','maxSimultaneousGames':6}
        settings = {f"{k}={v}" for k,v in settings_dict.items()}
        self.command = shlex.split(f'"{exe}" contribute -config "{cfg}" -base-dir "{base_dir}" -override-config "{",".join(settings)}"')
        self.start()

    @staticmethod
    def game_ended(game):
        cn = game.current_node
        if cn.is_pass and cn.analysis_exists:
            moves = cn.candidate_moves
            if moves and moves[0]["move"] == "pass":
                game.play(Move(None, player=game.current_node.next_player))  # play pass
        return game.end_result

    def advance_showing_game(self):
        current_game = self.active_games.get(self.showing_game)
        if current_game:
            end_result = self.game_ended(current_game)
            if end_result is not None:
                self.finished_games.add(self.showing_game)
                if time.time() - self.last_advance > self.SHOW_RESULT_TIME:
                    del self.active_games[self.showing_game]
                    if self.save_sgf:
                        filename = f"./dist_sgf/{self.showing_game}.sgf"
                        self.katrain.log(current_game.write_sgf(filename, self.katrain.config("trainer")), OUTPUT_INFO)

                    self.katrain.log(f"Game {self.showing_game} finished, finding a new one", OUTPUT_INFO)
                    self.showing_game = None
            elif time.time() - self.last_advance > self.MOVE_SPEED or len(self.active_games) > self.MAX_BUFFER_GAMES:
                if current_game.current_node.children:
                    current_game.redo(1)
                    self.last_advance = time.time()
                    self.katrain("update-state")
                elif time.time() - self.last_advance > self.GIVE_UP_AFTER:
                    self.katrain.log(
                        f"Giving up on game {self.showing_game} which appears stuck, finding a new one", OUTPUT_INFO
                    )
                    del self.active_games[self.showing_game]
                    self.showing_game = None
        else:
            if self.active_games:
                self.showing_game = random.choice(list(self.active_games.keys()))
                for game_id, game in self.active_games.items():  # find finished game
                    if game.root.nodes_in_tree[-1].is_pass:
                        self.showing_game = game_id
                        break
                self.last_advance = time.time()
                self.katrain.log(f"Found new game to show: {self.showing_game}", OUTPUT_INFO)

                self.katrain.game = self.active_games[self.showing_game]
                self.katrain("update-state", redraw_board=True)

    def is_idle(self):
        return False

    def queries_remaining(self):
        return 1

    def start(self):
        try:
            self.katrain.log(f"Starting Distributed KataGo with {self.command}", OUTPUT_INFO)
            self.katago_process = subprocess.Popen(
                self.command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=self.shell,
            )
        except (FileNotFoundError, PermissionError, OSError) as e:
            self.katrain.log(
                i18n._("Starting Kata failed").format(command=self.command, error=e), OUTPUT_ERROR,
            )
            return  # don't start
        self.stdout_thread = threading.Thread(target=self._read_stdout_thread, daemon=True)
        self.stderr_thread = threading.Thread(target=self._read_stderr_thread, daemon=True)
        self.stdout_thread.start()
        self.stderr_thread.start()

    def check_alive(self, os_error="", exception_if_dead=False):
        ok = self.katago_process and self.katago_process.poll() is None
        if not ok and exception_if_dead:
            if self.katago_process:
                code = self.katago_process and self.katago_process.poll()
                if code == 3221225781:
                    died_msg = i18n._("Engine missing DLL")
                else:
                    os_error += f"status {code}"
                    died_msg = i18n._("Engine died unexpectedly").format(error=os_error)
                if code != 1:  # deliberate exit, already showed message?
                    self.katrain.log(died_msg, OUTPUT_ERROR)
                self.katago_process = None
            else:
                died_msg = i18n._("Engine died unexpectedly").format(error=os_error)
            raise EngineDiedException(died_msg)
        return ok

    def shutdown(self, finish=False):
        process = self.katago_process
        if process:
            self.katago_process = None
            process.terminate()
        for t in [self.stderr_thread, self.stdout_thread]:
            if t:
                t.join()

    def _read_stderr_thread(self):
        while self.katago_process is not None:
            try:
                line = self.katago_process.stderr.readline()
                if line:
                    try:
                        self.katrain.log(line.decode(errors="ignore").strip(), OUTPUT_KATAGO_STDERR)
                    except Exception as e:
                        print("ERROR in processing KataGo stderr:", line, "Exception", e)
                elif self.katago_process:
                    self.check_alive(exception_if_dead=True)
            except Exception as e:
                self.katrain.log(f"Exception in reading stdout {e}", OUTPUT_DEBUG)
                return

    def _read_stdout_thread(self):
        while self.katago_process is not None:
            try:
                line = self.katago_process.stdout.readline()
                if line:
                    line = line.decode(errors="ignore").strip()
                    if line.startswith("{"):
                        try:
                            analysis = json.loads(line)
                            if "gameId" in analysis:
                                game_id = analysis["gameId"]
                                if game_id in self.finished_games:
                                    continue
                                current_game = self.active_games.get(game_id)
                                new_game = current_game is None
                                if new_game:
                                    board_size = [analysis["boardXSize"], analysis["boardYSize"]]
                                    placements = {
                                        f"A{bw}": [
                                            Move.from_gtp(move, pl).sgf(board_size)
                                            for pl, move in analysis["initialStones"]
                                            if pl == bw
                                        ]
                                        for bw in "BW"
                                    }
                                    game_properties = {k: v for k, v in placements.items() if v}
                                    game_properties["SZ"] = f"{board_size[0]}:{board_size[1]}"
                                    game_properties["KM"] = analysis["rules"]["komi"]
                                    game_properties["RU"] = json.dumps(analysis["rules"])
                                    game_properties["PB"] = analysis["blackPlayer"]
                                    game_properties["PW"] = analysis["whitePlayer"]
                                    current_game = BaseGame(self.katrain, game_properties=game_properties)
                                    self.active_games[game_id] = current_game
                                last_node = current_game.sync_branch(
                                    [Move.from_gtp(coord, pl) for pl, coord in analysis["moves"]]
                                )
                                last_node.set_analysis(analysis)
                                if new_game:
                                    current_game.set_current_node(last_node)
                                self.katrain.log(
                                    f"Game {game_id} Move {analysis['turnNumber']}: {' '.join(analysis['move'])}",
                                    OUTPUT_DEBUG,
                                )
                                self.katrain("update-state")
                        except Exception as e:
                            traceback.print_exc()
                            self.katrain.log(f"Exception {e} in parsing or processing JSON: {line}", OUTPUT_ERROR)
                    else:
                        self.katrain.log(line, OUTPUT_KATAGO_STDERR)
                elif self.katago_process:
                    self.check_alive(exception_if_dead=True)
            except Exception as e:
                self.katrain.log(f"Exception in reading stdout {e}", OUTPUT_DEBUG)
                return