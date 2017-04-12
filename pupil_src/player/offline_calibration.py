'''
(*)~---------------------------------------------------------------------------
Pupil - eye tracking platform
Copyright (C) 2012-2017  Pupil Labs

Distributed under the terms of the GNU
Lesser General Public License (LGPL v3.0).
See COPYING and COPYING.LESSER for license details.
---------------------------------------------------------------------------~(*)
'''

import os
import cv2
import numpy as np
from pyglui import ui
from plugin import Plugin
from player_methods import correlate_data
from methods import normalize
from video_capture import File_Source, EndofVideoFileError
from circle_detector import find_concetric_circles

from calibration_routines import Dummy_Gaze_Mapper
from calibration_routines.finish_calibration import finish_calibration

import background_helper as bh
from itertools import chain

import logging
logger = logging.getLogger(__name__)


class Global_Container(object):
    pass


def detect_marker_positions(cmd_pipe, data_pipe, source_path, timestamps_path):
    timestamps = np.load(timestamps_path)
    min_ts = timestamps[0]
    max_ts = timestamps[-1]

    src = File_Source(Global_Container(), source_path, timestamps, timed_playback=False)
    frame = src.get_frame()

    logger.info('Starting calibration marker detection...')

    try:
        while True:
            for event in bh.recent_events(cmd_pipe):
                if event == bh.TERM_SIGNAL:
                    raise RuntimeError()

            progress = 100 * (frame.timestamp - min_ts) / (max_ts - min_ts)
            cmd_pipe.send(('progress', progress))

            gray_img = frame.gray
            markers = find_concetric_circles(gray_img, min_ring_count=3)
            if len(markers) > 0:
                detected = True
                marker_pos = markers[0][0][0]  # first marker innermost ellipse, pos
                pos = normalize(marker_pos, (frame.width, frame.height), flip_y=True)

            else:
                detected = False
                pos = None

            if detected:
                second_ellipse = markers[0][1]
                col_slice = int(second_ellipse[0][0]-second_ellipse[1][0]/2),int(second_ellipse[0][0]+second_ellipse[1][0]/2)
                row_slice = int(second_ellipse[0][1]-second_ellipse[1][1]/2),int(second_ellipse[0][1]+second_ellipse[1][1]/2)
                marker_gray = gray_img[slice(*row_slice),slice(*col_slice)]
                avg = cv2.mean(marker_gray)[0]
                center = marker_gray[int(second_ellipse[1][1])//2, int(second_ellipse[1][0])//2]
                rel_shade = center-avg

                ref = {}
                ref["norm_pos"] = pos
                ref["screen_pos"] = marker_pos
                ref["timestamp"] = frame.timestamp
                ref['index'] = frame.index
                if rel_shade > 30:
                    ref['type'] = 'stop_marker'
                else:
                    ref['type'] = 'calibration_marker'

                data_pipe.send(ref)
            frame = src.get_frame()

    except (EndofVideoFileError, RuntimeError, EOFError, OSError, BrokenPipeError):
        pass
    finally:
        cmd_pipe.send(('finished',))  # one-element tuple required
        cmd_pipe.close()
        data_pipe.close()


def map_pupil_positions(cmd_pipe, data_pipe, pupil_list, gaze_mapper_cls, kwargs):
    try:
        gaze_mapper = gaze_mapper_cls(Global_Container(), **kwargs)
        for idx, datum in enumerate(pupil_list):
            for event in bh.recent_events(cmd_pipe):
                if event == bh.TERM_SIGNAL:
                    raise RuntimeError()

            mapped_gaze = gaze_mapper.on_pupil_datum(datum)
            if mapped_gaze:
                data_pipe.send(mapped_gaze)
                progress = 100 * (idx+1)/len(pupil_list)
                cmd_pipe.send(('progress', progress))

    except (RuntimeError, EOFError, OSError, BrokenPipeError):
        pass
    finally:
        cmd_pipe.send(('finished',))  # one-element tuple required
        cmd_pipe.close()
        data_pipe.close()


class Offline_Calibration(Plugin):
    def __init__(self, g_pool):
        super().__init__(g_pool)
        self.ref_positions = []
        self.gaze_positions = []
        self.original_gaze_pos_by_frame = self.g_pool.gaze_positions_by_frame

        self.g_pool.detection_mapping_mode = '3d'
        self.g_pool.plugins.add(Dummy_Gaze_Mapper)
        self.g_pool.active_calibration_plugin = self

        self.mapping_progress = 0.
        self.mapping_proxy = None
        self.detection_proxy = None
        self.start_detection_task()

    def start_detection_task(self):
        # cancel current detection if running
        self.detection_progress = 0.0
        bh.cancel_background_task(self.detection_proxy, False)

        source_path = self.g_pool.capture.source_path
        timestamps_path = os.path.join(self.g_pool.rec_dir, "world_timestamps.npy")

        self.detection_proxy = bh.start_background_task(detect_marker_positions,
                                                        name='Calibration Marker Detection',
                                                        args=(source_path, timestamps_path))

    def start_mapping_task(self):
        # cancel current mapping if running
        self.mapping_progress = 0.
        bh.cancel_background_task(self.mapping_proxy, False)

        pupil_list = self.g_pool.pupil_data['pupil_positions']
        gaze_mapper_cls = type(self.g_pool.active_gaze_mapping_plugin)
        gaze_mapper_kwargs = self.g_pool.active_gaze_mapping_plugin.get_init_dict()

        self.mapping_proxy = bh.start_background_task(map_pupil_positions,
                                                      name='Gaze Mapping',
                                                      args=(pupil_list, gaze_mapper_cls, gaze_mapper_kwargs))

    def init_gui(self):
        if not hasattr(self.g_pool, 'sidebar'):
            # Will be required when loading gaze mappers
            self.g_pool.sidebar = ui.Scrolling_Menu("Sidebar", pos=(-660, 20), size=(300, 500))
            self.g_pool.gui.append(self.g_pool.sidebar)

        def close():
            self.alive = False
        self.menu = ui.Growing_Menu("Offline Calibration")
        self.g_pool.sidebar.insert(0, self.menu)
        self.menu.append(ui.Button('Close', close))

        slider = ui.Slider('detection_progress', self, label='Detection Progress')
        slider.display_format = '%3.0f%%'
        slider.read_only = True
        self.menu.append(slider)

        slider = ui.Slider('mapping_progress', self, label='Mapping Progress')
        slider.display_format = '%3.0f%%'
        slider.read_only = True
        self.menu.append(slider)
        # self.menu.append(ui.Button('Redetect', self.redetect))

    def deinit_gui(self):
        if hasattr(self, 'menu'):
            self.g_pool.sidebar.remove(self.menu)
            self.menu = None

    def get_init_dict(self):
        return {}

    def on_notify(self, notification):
        if notification['subject'] == 'pupil_positions_changed' and not self.detection_proxy:
            self.calibrate()  # do not calibrate while detection task is still running
        elif notification['subject'] == 'calibration.successful':
            logger.info('Offline calibration successful. Starting mapping...')
            self.start_mapping_task()

    def recent_events(self, events):
        if self.detection_proxy:
            for ref_pos in bh.recent_events(self.detection_proxy.data):
                self.ref_positions.append(ref_pos)
            for msg in bh.recent_events(self.detection_proxy.cmd):
                if msg[0] == 'progress':
                    self.detection_progress = msg[1]
                elif msg[0] == 'finished':
                    self.detection_proxy = None
                    self.calibrate()

        if self.mapping_proxy:
            for mapped_gaze in bh.recent_events(self.mapping_proxy.data):
                self.gaze_positions.extend(mapped_gaze)
            for msg in bh.recent_events(self.mapping_proxy.cmd):
                if msg[0] == 'progress':
                    self.mapping_progress = msg[1]
                elif msg[0] == 'finished':
                    self.mapping_proxy = None
                    self.finish_mapping()

    def calibrate(self):
        if not self.ref_positions:
            logger.error('No markers have been found. Cannot calibrate.')
            return

        first_idx = self.ref_positions[0]['index']
        last_idx = self.ref_positions[-1]['index']
        pupil_list = list(chain(*self.g_pool.pupil_positions_by_frame[first_idx:last_idx]))
        finish_calibration(self.g_pool, pupil_list, self.ref_positions)

    def finish_mapping(self):
        self.g_pool.pupil_data['gaze_positions'] = self.gaze_positions
        self.g_pool.gaze_positions_by_frame = correlate_data(self.gaze_positions, self.g_pool.timestamps)
        self.notify_all({'subject': 'gaze_positions_changed'})

    def cleanup(self):
        bh.cancel_background_task(self.detection_proxy)
        bh.cancel_background_task(self.mapping_proxy)
        self.g_pool.gaze_positions_by_frame = self.original_gaze_pos_by_frame
        self.notify_all({'subject': 'gaze_positions_changed'})
        self.deinit_gui()
        self.g_pool.active_gaze_mapping_plugin.alive = False
        del self.g_pool.detection_mapping_mode
        del self.g_pool.active_calibration_plugin