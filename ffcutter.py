#!/bin/python3
import os
import sys
import math
import re
import signal
import locale
import subprocess
import collections
import tempfile
import shutil
import json
import traceback
import hashlib
import threading

import colorama
from docopt import docopt
from PyQt5 import QtGui, QtCore, QtWidgets
from PyQt5.QtCore import Qt

from mpv import MPV
from gui import Ui_root


doc = """ffcutter

Usage:
    ffcutter <video-file> [-s <save-file> --mpv=mpv-option...]
    ffcutter -h | --help

Options:
    -s <save-file>          Specify save file. Default is "filename.ffcutter" inside working directory.
    -m --mpv mpv-option     Specify additional mpv option or change the default ones.

Examples:
    ffcutter ./movie.mkv
    ffcutter ./movie.mkv -s ./movie.mkv.ffcutter
    ffcutter ./movie.mkv -m hr-seek=yes -m wid=-1

Default mpv options:
    wid=$wid
    keep-open=yes
    rebase-start-time=no
    framedrop=no
    osd-level=2
    osd-fractions=yes

Program state is saved into the save file on every user action.

GUI keys:
    space - Play/pause.
    arrows - Step frames.
    ctrl + arrows - Step seconds.
    shift + arrows - Jump keyframes.
    alt + arrows - Jump anchors.
    up/down arrows - Step 5%.
    [] - Jump chapters if any.

    z - Put anchor on the current playback position.
    x - Remove highlighted anchor.

    k - Show keyframes on the seekbar.
    i - Print input file information to the terminal.
    o - Open resulted file.
    ctrl + o - Open its directory.

If program crashes try to rerun it (duh).
"""

# TODO
# find stream copy accuracy offsets by trial end error


class GUI(QtWidgets.QDialog):

    statusbar_update = QtCore.pyqtSignal()
    player_loaded = QtCore.pyqtSignal()
    frameindex_built = QtCore.pyqtSignal()
    shell_message = QtCore.pyqtSignal(str)

    def __init__(self, filename, save_filename=None, mpv_options=[], skip_index=False):
        super().__init__()

        self.filename = filename
        self.save_filename = save_filename or os.path.split(filename)[1] + '.ffcutter'
        self.mpv_options = mpv_options

        self.hover_cursor = None # mouse position on the seek seekbar
        self.playback_pos = None
        self.playback_len = None
        self.segments = []
        self.anchor = None # single anchor position that hasn't become a segment
        self.closest_anchor = None # self.anchor or anchor closest to playback_pos

        self.show_keyframes = False
        self.running_ffmpeg = False

        self.tmpdir = os.path.join(tempfile.gettempdir(), 'ffcutter')
        try:
            os.mkdir(self.tmpdir)
        except FileExistsError:
            pass
        except Exception:
            self.tmpdir = tempfile.gettempdir()

        colorama.init()

        # set up the user interface from Designer
        self.ui = Ui_root()
        self.ui.setupUi(self)
        self.setWindowTitle('ffcutter - ' + os.path.split(filename)[-1])
        self.setFocus(True)

        self.ui.remove.toggled.connect(self.save_state)
        self.ui.keep.toggled.connect(self.save_state)
        self.ui.twoPass.toggled.connect(self.save_state)
        self.ui.encode.toggled.connect(self.save_state)
        self.ui.print.clicked.connect(self.print_ffmpeg)
        self.ui.run.clicked.connect(self.run_ffmpeg)

        editor = self.ui.argsEdit
        text = editor.toPlainText()

        outfile = self.get_user_ffmpeg_args()[0]
        text = re.sub(r'out:[^\S\n]*\n', 'out: %s\n' % outfile, text)
        editor.setPlainText(text)

        editor.hide()
        editor.textChanged.connect(self.save_state)
        self.ui.toggleArgsEdit.clicked.connect(lambda: editor.setHidden(not editor.isHidden()))

        self.ui.twoPass.setEnabled(False)
        self.ui.encode.toggled.connect(lambda: self.ui.twoPass.setEnabled(self.ui.encode.isChecked()))

        self.statusbar_update.connect(self.update_statusbar)

        self.seekbar_pressed = False
        self.ui.seekbar.paintEvent = self.seekbar_paint_event
        self.ui.seekbar.mouseMoveEvent = self.seekbar_mouse_move_event
        self.ui.seekbar.mousePressEvent = self.seekbar_mouse_press_event
        self.ui.seekbar.mouseReleaseEvent = self.seekbar_mouse_release_event
        self.ui.seekbar.leaveEvent = self.seekbar_leave_event

        self.refresh_statusbar_timer = QtCore.QTimer(self)
        self.refresh_statusbar_timer.setInterval(300)
        self.refresh_statusbar_timer.timerEvent = lambda _: self.update_statusbar()

        # check if necessary binaries are present

        self.ffmpeg_bin = 'ffmpeg'
        self.ffprobe_bin = 'ffprobe'
        if os.name == 'nt':
            dirname = os.path.split(__file__)[0]
            self.ffmpeg_bin = os.path.join(dirname, 'ffmpeg.exe')
            self.ffprobe_bin = os.path.join(dirname, 'ffprobe.exe')

        if not shutil.which(self.ffmpeg_bin):
            self.print_error('FFmpeg weren\'t found.')
            self.ui.run.setEnabled(False)
            self.ffmpeg_bin = None
        if not shutil.which(self.ffprobe_bin):
            self.print_error('FFprobe weren\'t found. Wont be able to build frame index.')
            self.ffprobe_bin = None

        # get frames timestamps and find keyframes
        # inside separate thread because long blocking calls crash qt application

        # also check if ffmpeg stream copy seeking is ok (I had this problem on a few mkv files)
        # shift variables hold the number on which
        # each segment should be shifted to achieve accuracy
        # TODO: implement or get rid of this trickery if possible

        self.pts = []
        self.ipts = []
        self.ffmpeg_seeking_problem = False
        self.ffmpeg_shift_a = 0
        self.ffmpeg_shift_b = 0

        def on_frameindex_built():
            self.show()
            self.init_player()

        if not self.ffprobe_bin or skip_index:
            on_frameindex_built()
        else:
            self.frameindex_built.connect(on_frameindex_built)
            threading.Thread(target=self.load_ffmpeg_frames_info).start()

        # SIGINT handling trickery

        timer = QtCore.QTimer(self)
        timer.timerEvent = lambda _: None
        timer.start(1000)
        self.interrupted = False

    def interrupt(self):
        if self.running_ffmpeg:
            self.interrupted = True
        else:
            self.print('Exiting gracefully.')
            QtWidgets.QApplication.quit()

    def check_ffmpeg_seek_problem(self):
        self.print('Testing if ffmpeg stream copy seeking on this file works correctly...')

        def clean():
            for f in [first_frame1, first_frame2, tmpfile]:
                try:
                    os.remove(f)
                except Exception:
                    pass

        first_frame1 = os.path.join(self.tmpdir, 'sample1.png')
        tmpfile = os.path.join(self.tmpdir, 'sample2' + os.path.splitext(self.filename)[1])
        first_frame2 = os.path.join(self.tmpdir, 'sample2.png')

        errmsg = 'Failed testing ffmpeg.'

        # get frame with encoding on

        cmd = [self.ffmpeg_bin] + '-i FILE -y -frames 1 -v error'.split() + [first_frame1]
        cmd[2] = self.filename
        proc = subprocess.Popen(cmd)
        if self._wait(proc, errmsg):
            clean()
            return

        # get frame with encoding off and try to find offset

        # stream copy 1 frame video

        cmd = [self.ffmpeg_bin] + '-i FILE -y -ss TIME -c copy -frames 1 -v error'.split() + [tmpfile]
        cmd[2] = self.filename
        cmd[5] = str(self.playback_pos)

        proc = subprocess.Popen(cmd)
        if self._wait(proc, errmsg):
            clean()
            return

        # get that video first frame

        cmd = [self.ffmpeg_bin] + '-i -y -frames 1 -v error'.split() + [first_frame2]
        cmd.insert(2, tmpfile)
        proc = subprocess.Popen(cmd)
        if self._wait(proc, errmsg):
            clean()
            return

        with open(first_frame1, 'rb') as frame1, open(first_frame2, 'rb') as frame2:
            hash1 = hashlib.md5(frame1.read()).hexdigest()
            hash2 = hashlib.md5(frame2.read()).hexdigest()
            if hash1 != hash2:
                self.ffmpeg_seeking_problem = True
                self.print_error('FFmpeg stream copy seeking seem to work incorrectly.\n' +
                                 '    No-encode mode will most likely be inaccurate.')
            else:
                self.print('FFmpeg stream copy seeking seem to work correctly.')

        clean()

    def update_statusbar(self):
        if self.playback_pos is None:
            return

        seeking = self.player.seeking
        text = '<pre>{}{}{}</pre>'.format(
            'K ' if self.player.video_frame_info['picture-type'] == 'I' else '  ',
            format_time(floor(self.playback_pos, 3), full=True),
            ' ... ' if seeking else '',
            )

        if seeking and not self.refresh_statusbar_timer.isActive():
            self.refresh_statusbar_timer.start()
        elif not seeking and self.refresh_statusbar_timer.isActive():
            self.refresh_statusbar_timer.stop()

        self.ui.status.setText(text)

    def toggle_args_editor(self):
        editor = self.ui.argsEdit
        editor.setHidden(not editor.isHidden())

    # Frame index #################################################################################
    ###############################################################################################

    def _wait(self, proc, msg):
        if proc.wait() != 0:
            self.print_error('%s\n' % msg +
                             '    Command: %s\n' % ' '.join(proc.args) +
                             '    Exit code: %s' % proc.returncode)
        return proc.returncode

    def load_ffmpeg_frames_info(self):
        # get frames timestamps and find keyframes
        # (if this look like nonsense to you, sorry, I'm not very good in video processing)

        index_file = '%s.%s.frames' % (os.path.split(self.filename)[1], os.path.getsize(self.filename))
        index_file = os.path.join(self.tmpdir, index_file)

        if os.path.exists(index_file):
            self.print('Frames index loaded from %s' % index_file)
            with open(index_file) as f:
                self.pts, self.ipts = json.load(f)
        else:
            self.print('Building video frames index.')
            ret = self._load_timestamps_from_packets()
            if not ret:
                self.print('Building video frames index in full mode.')
                ret = self._load_timestamps_from_frames()

            if not ret:
                self.print_error('Filed building frames index.')
            else:
                self.pts, self.ipts = ret
                with open(index_file, 'w') as f:
                    json.dump([self.pts, self.ipts], f)

        self.frameindex_built.emit()

    def _load_timestamps_from_frames(self):

        # get video duration to be able to show progress

        frames_len = None

        cmd = [self.ffmpeg_bin] + '-i -c copy -f null'.split() + [os.devnull]
        cmd.insert(2, self.filename)
        proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        out = proc.stderr.read().decode()
        matches = re.findall(r'frame=\s*(\d+)', out)
        if matches:
            frames_len = int(matches[-1])

        # start the building process

        cmd = [self.ffprobe_bin] + '-show_frames -show_entries frame=best_effort_timestamp_time,pict_type -select_streams v -v error'.split()
        cmd.append(self.filename)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)

        def progress(n):
            msg = '\rProcessed: %s/%s' % (n, frames_len or '?')
            if frames_len:
                msg += ' (%d%%)' % (n/(frames_len/100))
            self.print(msg, end='')

        n = 0
        pts = []
        ipts = []
        while True:
            line = proc.stdout.readline()
            if not line:
                break

            if b'best_effort_timestamp_time=' in line:
                try:
                    pts.append(float(line.split(b'=')[1]))
                except Exception:
                    pass
                n += 1
                if n % 100 == 0:
                    progress(n)
            elif b'pict_type=I\n' == line:
                ipts.append(pts[-1])

        progress(n)
        self.print()

        if self._wait(proc, 'Failed building frames index.'):
            return

        if pts:
            return pts, ipts

    def _load_timestamps_from_packets(self):

        cmd = [self.ffprobe_bin] + '-show_packets -show_entries packet=pts_time,dts_time,flags -select_streams v -v error'.split()
        cmd.append(self.filename)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)

        pts = []
        dts = []
        ipts = []
        packetn = 0
        while True:
            line = proc.stdout.readline()
            if not line:
                break

            if b'=K' in line:
                # prefer pts
                try:
                    ipts.append(pts[packetn-1])
                except IndexError:
                    try:
                        ipts.append(dts[packetn-1])
                    except IndexError:
                        pass
            elif line[1:9] == b'ts_time=':
                if line.startswith(b'p'):
                    packetn += 1

                v = line.split(b'=')[1]
                try:
                    v = float(v)
                    if v >= 0:
                        if line.startswith(b'p'):
                            pts.append(v)
                        else:
                            dts.append(v)
                except ValueError:
                    pass

        if self._wait(proc, 'Failed building frames index.'):
            return

        pts = [t for t in sorted(set(pts + dts))]
        ipts = [t for t in sorted(set(ipts))]

        # filter out pts of incomplete packets
        q = collections.deque(maxlen=10)
        for t in sorted(set(pts)):
            rm = []
            for v in q:
                if abs(v-t) <= 0.002:
                    rm.append(v)
            for v in rm:
                q.remove(v)
                pts.remove(v)
                try:
                    ipts[ipts.index(v)] = t
                except Exception:
                    pass

        if pts:
            return pts, ipts

    # Info messages ###############################################################################
    ###############################################################################################

    def print(self, *args, **kw):
        print(*args, **kw)

    def print_error(self, *args, **kw):
        msg = colorama.Fore.LIGHTRED_EX + kw.get('sep', ' ').join([str(a) for a in args]) + colorama.Style.RESET_ALL
        print(msg, **kw)

    def print_segments(self):
        line = ' '.join(['%d-%d' % (a, b) for a, b in self.segments])
        if self.anchor is not None:
            line += ' (%d)' % self.anchor
        self.print(line)

    def print_video_info(self):
        proc = subprocess.run([self.ffmpeg_bin, '-i', self.filename], stderr=subprocess.PIPE)
        no = True
        self.print()
        term = shutil.get_terminal_size((80, 20))

        def sep(title=''):
            self.print('--' + title + '-' * (term.columns-2-len(title)))

        self.print()
        for line in proc.stderr.decode().splitlines():
            if line.startswith('Input'):
                no = False
            if no:
                continue

            if ': Video:' in line:
                self.print()
                sep('VIDEO')
                color = colorama.Fore.LIGHTCYAN_EX
                style = colorama.Style.BRIGHT
                reset = colorama.Style.RESET_ALL
                # we (I) are mostly interested in video bitrate, so
                line = re.sub(r'\b\d+\s+\w+/s\b', color + style + r'\g<0>' + reset, line)
                self.print(line)
            else:
                if ': Audio:' in line:
                    self.print()
                    sep('AUDIO')
                self.print(line)
        self.print()

    # Player ######################################################################################
    ###############################################################################################

    def init_player(self):

        def mpv_log(loglevel, component, message):
            self.print('Mpv log: [{}] {}: {}'.format(loglevel, component, message))

        mpv_args = []
        mpv_kw = {
            'wid': int(self.ui.video.winId()),
            'keep-open': 'yes',
            'rebase-start-time': 'no',
            'framedrop': 'no',
            'osd-level': '2',
            'osd-fractions': 'yes',
        }
        for opt in self.mpv_options:
            if '=' in opt:
                k, v = opt.split('=', 1)
                mpv_kw[k] = v
            else:
                mpv_args.append(opt)

        player = MPV(*mpv_args, log_handler=mpv_log, **mpv_kw)
        self.player = player
        player.pause = True

        def on_player_loaded():
            if self.ffmpeg_bin:
                self.check_ffmpeg_seek_problem()
            self.ui.loading.hide()
            if os.path.exists(self.save_filename):
                try:
                    with open(self.save_filename) as f:
                        state = json.load(f)
                        self.apply_state(state)
                except Exception:
                    self.print_error('Failed loading state file:')
                    traceback.print_exc()

        self.player_loaded.connect(on_player_loaded)

        def on_playback_len(s):
            self.playback_len = s
            player.unobserve_property('duration', on_playback_len)

        def on_playback_pos(s):
            if self.playback_pos is None:
                self.player_loaded.emit()
            self.playback_pos = s
            self.statusbar_update.emit()
            self.ui.seekbar.update()

        player.observe_property('time-pos', on_playback_pos)
        player.observe_property('duration', on_playback_len)
        player.play(self.filename)


    # Keyboard events #############################################################################
    ###############################################################################################

    def to_next_anchor(self, backwards=False):
        anchors = [t for ab in self.segments for t in ab]
        if self.anchor is not None:
            anchors.append(self.anchor)
            anchors = sorted(anchors)

        i = sidesi(self.playback_pos, anchors)[0 if backwards else 1]

        if i is not None:
            self.player.seek(anchors[i], 'absolute', 'exact')

    def to_next_keyframe(self, backwards=False):
        if not self.ipts:
            self.print('Couldn\'t get keyframes information')
            return

        i = sidesi(self.playback_pos, self.ipts, min_diff=1/self.player.fps)[0 if backwards else 1]

        if i is not None:
            # TODO sometimes such mpv exact seek fail, fix it somehow
            self.player.seek(self.ipts[i], 'absolute', 'exact')

    def keyPressEvent(self, event):
        k = event.key()
        ctrl = event.modifiers() == Qt.ControlModifier
        alt = event.modifiers() == Qt.AltModifier
        shift = event.modifiers() == Qt.ShiftModifier

        if ctrl and k in (Qt.Key_Q, Qt.Key_W):

            QtWidgets.QApplication.quit()

        elif k == Qt.Key_D:

            try:
                import ptpdb
                ptpdb.set_trace()
            except ImportError:
                import pdb
                pdb.set_trace()

        elif k == Qt.Key_O:

            outfile = self.get_user_ffmpeg_args()[0]
            try:
                if not ctrl:
                    default_open(outfile)
                else:
                    default_open(os.path.split(outfile)[0] or '.')
            except Exception as e:
                self.print_error(e)

        elif k == Qt.Key_Escape:

            self.setFocus(True)

        elif k == Qt.Key_I:

            self.print_video_info()

        ################################

        if self.playback_pos is None:
            return

        ################################

        if k == Qt.Key_Space:

            self.player.pause = not self.player.pause

        elif k == Qt.Key_BracketRight:

            self.player.command('add', 'chapter', 1)

        elif k == Qt.Key_BracketLeft:

            self.player.command('add', 'chapter', -1)

        elif k == Qt.Key_Up:

            self.player.seek(5, 'relative-percent')

        elif k == Qt.Key_Down:

            self.player.seek(-5, 'relative-percent')

        elif k == Qt.Key_Left:

            if ctrl:
                self.player.seek(-1, 'relative', 'exact')
            elif alt:
                self.to_next_anchor(True)
            elif shift:
                self.keyframe_jumped = True
                self.to_next_keyframe(True)
            else:
                self.player.frame_back_step()

        elif k == Qt.Key_Right:

            if ctrl:
                self.player.seek(1, 'relative', 'exact')
            elif alt:
                self.to_next_anchor()
            elif shift:
                self.keyframe_jumped = True
                self.to_next_keyframe()
            else:
                self.player.frame_step()

        elif k == Qt.Key_Z:

            self.put_anchor()

        elif k == Qt.Key_X:

            self.del_anchor()

        elif k == Qt.Key_K:

            if not self.show_keyframes and not self.ipts:
                self.print('No keyframes information.')
            self.show_keyframes = not self.show_keyframes
            self.ui.seekbar.update()


        self.update_statusbar()

    def del_anchor(self):
        if self.closest_anchor == self.anchor:
            self.anchor = None
        else:
            for a, b in self.segments:
                if a == self.closest_anchor:
                    self.segments.remove((a, b))
                    self.anchor = b
                elif b == self.closest_anchor:
                    self.segments.remove((a, b))
                    self.anchor = a

        self.print('> del, %s segments' % len(self.segments))
        self.print_segments()
        self.save_state()
        self.ui.seekbar.update()

    def put_anchor(self, split_if_inside=True):
        if self.anchor is None:
            self.anchor = self.playback_pos
            move = 0
        else:
            aa, bb = self.anchor, self.playback_pos
            if aa > bb:
                aa, bb = bb, aa

            aai = -1
            bbi = -1

            for i, seg in enumerate(self.segments):
                a, b = seg

                if a <= aa <= b:
                    aai = i
                if a <= bb <= b:
                    bbi = i

            def remove_between(aa, bb):
                segments = []
                for a, b in self.segments:
                    if not (a >= aa and b <= bb):
                        segments.append((a, b))
                self.segments = segments

            if aai == -1 and bbi == -1:
                move = 1
                # both sides on clean range

                remove_between(aa, bb)
                self.segments.append((aa, bb))

            elif aai > -1 and aai == bbi:
                move = 2
                # fully inside another segment -> split that segment

                a, b = self.segments.pop(aai)
                if a == aa:
                    self.segments.append((bb, b))
                elif b == bb:
                    self.segments.append((a, aa))
                else:
                    self.segments.extend([(a, aa), (bb, b)])

            elif (aai != -1 and bbi == -1) or (aai == -1 and bbi != -1):
                move = 3
                # only one side inside another segment

                if aai > -1:
                    aa, _ = self.segments.pop(aai)
                else:
                    _, bb = self.segments.pop(bbi)
                remove_between(aa, bb)
                self.segments.append((aa, bb))

            elif aai != bbi and split_if_inside:
                move = 4
                # both sides on different segments -> join those segments

                a, _ = self.segments.pop(aai)
                _, b = self.segments.pop(bbi-1)
                remove_between(aa, bb)

                self.segments.append((a, b))

            self.anchor = None

        self.segments = list(sorted(self.segments, key=lambda t: t[0]))

        self.print('> put, move №%s, %s segments' % (move, len(self.segments)))
        self.print_segments()
        self.save_state()
        self.ui.seekbar.update()

    # File state ##################################################################################
    ###############################################################################################

    def get_state(self):
        return {
            'mode': 'keep' if self.ui.keep.isChecked() else 'remove',
            'segments': self.segments,
            'anchor': self.anchor,
            'ffargs': self.ui.argsEdit.toPlainText(),
            'encode': self.ui.encode.isChecked(),
            '2-pass': self.ui.twoPass.isChecked(),
        }

    def apply_state(self, state):
        if state.get('mode') == 'keep':
            self.ui.keep.setChecked(True)
        elif state.get('mode') == 'remove':
            self.ui.remove.setChecked(True)

        for a, b in state.get('segments', []):
            self.playback_pos = a
            self.put_anchor()
            self.playback_pos = b
            self.put_anchor(split_if_inside=False)
        self.playback_pos = 0

        text = state.get('ffargs')
        if text:
            self.ui.argsEdit.setPlainText(text)

        self.ui.encode.setChecked(state.get('encode', False))

        self.ui.twoPass.setChecked(state.get('2-pass', False))
        self.ui.twoPass.setEnabled(self.ui.encode.isChecked())

        self.anchor = state['anchor']

        self.ui.seekbar.update()

    def save_state(self):
        try:
            with open(self.save_filename, 'w') as f:
                json.dump(self.get_state(), f, indent=True, sort_keys=True)
        except IOError as e:
            self.print_error(e)

    # Encoding ####################################################################################
    ###############################################################################################

    def get_inversed_segments(self):
        segments = []
        anchors = [t for seg in self.segments for t in seg]
        prev = None
        for i, t in enumerate(anchors):
            if i % 2 == 0: # a
                if prev is None:
                    prev = 0
                first_frame = (t == 0 or
                               (self.pts and closest(t, self.pts) == self.pts[0]))

                if not first_frame:
                    segments.append((prev, t))
            else: # b
                prev = t
                if i == len(anchors)-1:
                    last_frame = ((self.playback_len - t) < (1/self.player.fps) or
                                  (self.pts and closest(t, self.pts) == self.pts[-1]))

                    if not last_frame:
                        segments.append((t, self.playback_len))

    def adjust_segements(self, segments):

        frame_duration = 1/self.player.fps
        keep = self.ui.keep.isChecked()
        encode = self.ui.encode.isChecked()

        for i, seg in enumerate(segments):
            a, b = seg

            if keep:
                b += frame_duration
            else:
                a += frame_duration

            #     a += frame_duration * self.ffmpeg_copy_frames_shift_a
            #     b += frame_duration * self.ffmpeg_copy_frames_shift_b

            a = closest(a, self.pts, max_diff=frame_duration) or a
            b = closest(b, self.pts, max_diff=frame_duration) or b

            segments[i] = (round(a, 3), round(b, 3))

    def get_user_ffmpeg_args(self):
        outfile = None
        outargs = []
        inargs = []
        for line in self.ui.argsEdit.toPlainText().splitlines():
            line = line.strip()
            if line.startswith('out:'):
                outfile = line[4:].strip()
            elif line.startswith('out-args:'):
                outargs = line[9:].strip().split()
                outargs = map(str.strip, outargs)
                outargs = [arg for arg in outargs if arg and not arg.startswith('#')]
            elif line.startswith('in-args:'):
                inargs = line[8:].strip().split()
                inargs = map(str.strip, inargs)
                inargs = [arg for arg in inargs if arg and not arg.startswith('#')]

        if not outfile:
            orig_name, ext = os.path.splitext(os.path.split(self.filename)[1])
            outfile = orig_name + '.ffcutter' + ext

        outargs = outargs or []
        inargs = inargs or []

        return outfile, outargs, inargs

    def make_ffmpeg(self):

        outfile, outargs, inargs = self.get_user_ffmpeg_args()

        # Configurations ############################################
        #############################################################

        path_name, ext = os.path.splitext(outfile)
        just_name = os.path.split(path_name)[1]
        tmpfiles = []
        keep = self.ui.keep.isChecked()
        encode = self.ui.encode.isChecked()
        twoPass = self.ui.twoPass.isChecked()

        # Compiling "encode segments to intermediate files" command #
        #############################################################

        encode_commands = []

        if keep:
            segments = self.segments.copy()
        else:
            segments = self.get_inversed_segments()

        self.adjust_segements(segments)

        for _ in segments:
            tmpfile = '%s.part%03d%s' % (path_name, len(tmpfiles), ext)
            tmpfiles.append(tmpfile)

        # generate the commands

        ffmpeg = self.ffmpeg_bin or 'ffmpeg'

        if not encode:

            encode_command = [ffmpeg] + inargs + ['-i', self.filename, '-y']
            for i, seg in enumerate(segments):
                a, b = seg
                encode_command += ['-ss', str(a), '-to', str(b), '-c', 'copy'] + outargs + [tmpfiles[i]]
            encode_commands.append(encode_command)

        elif twoPass:

            passlogfiles = [os.path.join(self.tmpdir, tmpfile) for tmpfile in tmpfiles]

            encode_command = [ffmpeg] + inargs + ['-i', self.filename, '-y']
            for i, seg in enumerate(segments):
                a, b = seg
                if '-f' not in outargs:
                    encode_command += ['-f', ext[1:].lower()]
                encode_command += ['-ss', str(a), '-to', str(b), '-an', '-pass', '1', '-passlogfile', passlogfiles[i]] + outargs + [os.devnull]
            encode_commands.append(encode_command)

            encode_command = [ffmpeg] + inargs + ['-i', self.filename, '-y']
            for i, seg in enumerate(segments):
                a, b = seg
                encode_command += ['-ss', str(a), '-to', str(b), '-pass', '2', '-passlogfile', passlogfiles[i]] + outargs + [tmpfiles[i]]
            encode_commands.append(encode_command)

        else:

            encode_command = [ffmpeg] + inargs + ['-i', self.filename, '-y']
            for i, seg in enumerate(segments):
                a, b = seg
                encode_command += ['-ss', str(a), '-to', str(b)] + outargs + [tmpfiles[i]]
            encode_commands.append(encode_command)

        # Compiling "concatenate intermediate files" command ########
        #############################################################

        list_file = os.path.join(self.tmpdir, just_name + ext + '.parts')
        with open(list_file, 'w') as f:
            for file in tmpfiles:
                f.write('file \'%s\'\n' % os.path.abspath(file).replace("'", "'\\''"))

        concat_command = [ffmpeg, '-f', 'concat', '-safe', '0', '-i', list_file, '-y', '-c', 'copy', outfile]

        #############################################################

        return encode_commands + [concat_command]

    def print_ffmpeg(self):
        self.print()
        for args in self.make_ffmpeg():
            self.print(' '.join(args))
        self.print()

    def run_ffmpeg(self):
        commands = self.make_ffmpeg()
        commands_len = len(commands)

        self.print()
        for i, args in enumerate(commands, 1):
            self.print(i, ' '.join(args))

        self._proc = None

        def next_run():
            args = commands.pop(0)
            self.print()
            self.print('%d/%d - %s' % (commands_len - len(commands), commands_len, ' '.join(args)))
            self._proc = subprocess.Popen(args)

        def stop(exit_code):
            self.ui.run.setEnabled(True)
            self.running_ffmpeg = False
            timer.stop()
            self.print()
            if self.interrupted:
                self.print_error('Interrupted. Command exit code: %s' % exit_code)
                self.interrupted = False
            elif exit_code == 0:
                self.print('Done.')
            else:
                self.print_error('Fail. Command exit code: %s' % exit_code)

        def check(_):
            code = self._proc.poll()
            if code is not None:
                if code != 0 or not commands:
                    stop(code)
                else:
                    next_run()
            elif self.interrupted:
                self._proc.send_signal(signal.SIGINT)

        self.running_ffmpeg = True
        next_run()

        timer = QtCore.QTimer(self)
        timer.timerEvent = check
        timer.setInterval(1000)
        timer.start()
        self.ui.run.setEnabled(False)

    # Bar #########################################################################################
    ###############################################################################################

    def seekbar_mouse_move_event(self, event):
        self.hover_cursor = event.x()
        if self.seekbar_pressed:
            self.seekbar_mouse_press_event(event)
        self.ui.seekbar.update()

    def seekbar_leave_event(self, event):
        self.hover_cursor = None
        self.ui.seekbar.update()

    def seekbar_mouse_press_event(self, event):
        if self.playback_pos is None:
            return

        self.seekbar_pressed = True
        precision = 'exact' if event.modifiers() == Qt.ControlModifier else None
        self.player.seek(event.x() / (self.ui.seekbar.width() / 100), 'absolute-percent', precision)
        self.ui.seekbar.update()

    def seekbar_mouse_release_event(self, event):
        self.seekbar_pressed = False

    def seekbar_paint_event(self, event):
        if self.playback_pos is None:
            return

        seekbar = self.ui.seekbar
        painter = QtGui.QPainter(seekbar)

        playback_inside_segment = self.playback_pos == self.anchor
        closest_anchor = None
        closest_anchor_diff = self.playback_len

        def time_to_x(s):
            return seekbar.width() * s / self.playback_len

        # segments
        color = QtGui.QColor(0xC36DCB)
        for a, b in self.segments:
            if a <= self.playback_pos <= b:
                playback_inside_segment = True

            diff = abs(a - self.playback_pos)
            if diff < closest_anchor_diff:
                closest_anchor = a
                closest_anchor_diff = diff

            diff = abs(b - self.playback_pos)
            if diff < closest_anchor_diff:
                closest_anchor = b
                closest_anchor_diff = diff

            a, b = time_to_x(a), time_to_x(b)
            w = b - a
            if w < 1:
                w = 1
            painter.fillRect(a, 0, w, seekbar.height(), color)

        # playback cursor
        painter.setPen(QtGui.QColor(Qt.black))
        a = time_to_x(self.playback_pos)
        painter.drawLine(a, 0, a, seekbar.height())

        # playback cursor inside segment indicator
        if playback_inside_segment:
            size = 5
            half = 2
            painter.fillRect(a-half, seekbar.height()/2-half, size, size, QtGui.QColor(0,0,0,200))

        # single anchor
        if self.anchor is not None:
            closest_anchor = self.anchor

            color = QtGui.QColor(Qt.cyan)
            painter.setPen(color)
            pos = time_to_x(self.anchor)
            painter.drawLine(pos, 0, pos, seekbar.height())

        # closest anchor highlight
        if closest_anchor is not None:
            pos = time_to_x(closest_anchor)

            painter.setPen(Qt.NoPen)
            painter.setBrush(Qt.darkGreen)

            h = 6
            halfw = 4
            p1 = QtCore.QPoint(pos, seekbar.height()-h)
            p2 = QtCore.QPoint(pos-halfw, seekbar.height())
            p3 = QtCore.QPoint(pos+halfw, seekbar.height())
            painter.drawPolygon(p1, p2, p3)

        self.closest_anchor = closest_anchor

        # hover cursor
        if self.hover_cursor is not None:
            painter.setPen(QtGui.QColor(0,0,0,90))
            painter.drawLine(self.hover_cursor, 0, self.hover_cursor, seekbar.height())

        # chapters
        if self.player.chapter_list:
            painter.setPen(Qt.black)
            for ch in self.player.chapter_list:
                x = time_to_x(ch['time'])
                painter.drawPoint(x, 0)
                painter.drawPoint(x-1, 0)
                painter.drawPoint(x+1, 0)
                painter.drawPoint(x, 1)

        # debug keyframes
        if self.show_keyframes:
            painter.setPen(Qt.red)
            backwards = False
            y = 0
            for t in self.ipts:
                painter.drawPoint(time_to_x(t), y)
                painter.drawPoint(time_to_x(t), y+1)
                y += 3 if not backwards else -3
                if y > seekbar.height() - 3:
                    backwards = True
                    y -= 6
                elif y < 0:
                    backwards = False
                    y += 6


def sidesi(target, sorted_elements, min_diff=0, max_diff=None):
    'sidesi(5, [3,4,5,6]) -> (1, 3)'

    t = target
    ls = sorted_elements
    log = False

    for i, e in enumerate(ls):
        # if log:
        #     print('--', t, e)

        if e >= target:
            a = b = None

            #########################

            ri = i
            while True:
                if ri == len(ls):
                    break

                d = ls[ri] - t
                if d != 0 and min_diff <= d:
                    if max_diff is not None and d > max_diff:
                        ri += 1
                        continue
                    b = ri
                    break

                ri += 1


            #########################

            li = i-1
            while True:
                if li == -1:
                    break

                d = t - ls[li]
                if d != 0 and min_diff <= d:
                    if max_diff is not None and d > max_diff:
                        li -= 1
                        continue
                    a = li
                    break

                li -= 1


            #########################

            if log:
                print('return:', a, t, b)

            return (a, b)
    else:
        if not ls:
            return (None, None)

        a = b = None

        li = len(ls) - 1
        while True:
            if li == -1:
                break

            d = t - ls[li]
            if d != 0 and min_diff <= d:
                if max_diff is not None and d > max_diff:
                    li -= 1
                    continue
                a = li
                break

            li -= 1

        if log:
            print('else  :', a, t, b)

        return (a, b)


def sides(target, elements, **kw):
    ai, bi = sidesi(target, elements, **kw)
    a = b = None
    if ai:
        a = elements[ai]
    if bi:
        b = elements[bi]
    return (a, b)


def closest(target, elements, max_diff=None):
    try:
        el = min(elements, key=lambda e: abs(target-e))
        if max_diff is None or abs(el - target) < max_diff:
            return el
    except ValueError:
        pass


def floor(number, ndigits=0):
    if not ndigits:
        return math.floor(number)
    else:
        m = 10**ndigits
        return math.floor(number*m)/m


def format_time(seconds, full=False):
    l = ''
    s = seconds

    h = int(s/3600)
    if full or h:
        s = s-3600*h
        l += '%02d:' % h

    m = int(s/60)
    if full or m:
        s = s-60*m
        l += '%02d:' % m
    elif h:
        l += '00:'

    if full or s < seconds:
        l += '%02d' % s
    else:
        l += '%d' % s

    dec = s % 1
    if full or dec:
        l += ('%.3f' % dec)[1:]

    return l


def parse_time(string):
    parts = string.split(':')
    if len(parts) == 1:
        return float(parts[0])
    elif len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    else:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])


def default_open(filepath):
    if sys.platform.startswith('darwin'):
        subprocess.Popen(('open', filepath), stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    elif os.name == 'nt':
        os.startfile(filepath)
    elif os.name == 'posix':
        subprocess.Popen(('xdg-open', filepath), stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    app = QtWidgets.QApplication(sys.argv)

    # for qt + debug
    QtCore.pyqtRemoveInputHook()

    # for qt + mpv
    locale.setlocale(locale.LC_NUMERIC, 'C')

    no_index = '--no-index' in sys.argv
    if no_index:
        sys.argv.remove('--no-index')
    args = docopt(doc)
    gui = GUI(args['<video-file>'], args['-s'], args['--mpv'], no_index)

    # for qt + ctrl-c
    signal.signal(signal.SIGINT, lambda *_: gui.interrupt())

    sys.exit(app.exec_())
