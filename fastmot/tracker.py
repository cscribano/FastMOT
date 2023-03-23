from types import SimpleNamespace
from collections import OrderedDict
import itertools
import logging
import numpy as np

from .track import Track
from .flow import Flow
from .kalman_filter import MeasType, KalmanFilter
from .utils.distance import Metric, cdist, iou_dist
from .utils.matching import linear_assignment, greedy_match, fuse_motion, gate_cost
from .utils.rect import as_tlbr, to_tlbr, ios, bbox_ious, find_occluded


LOGGER = logging.getLogger(__name__)


class MultiTracker:
    def __init__(self, size,
                 max_age=6,
                 age_penalty=2,
                 motion_weight=0.2,
                 iou_thresh=0.4,
                 duplicate_thresh=0.8,
                 occlusion_thresh=0.7,
                 conf_thresh=0.5,
                 confirm_hits=1,
                 history_size=50,
                 kalman_filter_cfg=None,
                 flow_cfg=None):
        """Class that uses KLT and Kalman filter to track multiple objects and
        associates detections to tracklets based on motion and appearance.

        Parameters
        ----------
        size : tuple
            Width and height of each frame.
        max_age : int, optional
            Max number of undetected frames allowed before a track is terminated.
            Note that skipped frames are not included.
        age_penalty : int, optional
            Scale factor to penalize KLT measurements for tracks with large age.
        motion_weight : float, optional
            Weight for motion term in matching cost function.
        iou_thresh : float, optional
            IoU threshold for association with unconfirmed and unmatched active tracks.
        duplicate_thresh : float, optional
            Track overlap threshold for removing duplicate tracks.
        occlusion_thresh : float, optional
            Detection overlap threshold for nullifying the extracted embeddings for association/reID.
        conf_thresh : float, optional
            Detection confidence threshold for starting a new track.
        confirm_hits : int, optional
            Min number of detections to confirm a track.
        history_size : int, optional
            Max size of track history to keep for reID.
        kalman_filter_cfg : SimpleNamespace, optional
            Kalman Filter configuration.
        flow_cfg : SimpleNamespace, optional
            Flow configuration.
        """
        self.size = size
        assert max_age >= 1
        self.max_age = max_age
        assert age_penalty >= 1
        self.age_penalty = age_penalty
        assert 0 <= motion_weight <= 1
        self.motion_weight = motion_weight
        assert 0 <= iou_thresh <= 1
        self.iou_thresh = iou_thresh
        assert 0 <= duplicate_thresh <= 1
        self.duplicate_thresh = duplicate_thresh
        assert 0 <= occlusion_thresh <= 1
        self.occlusion_thresh = occlusion_thresh
        assert 0 <= conf_thresh <= 1
        self.conf_thresh = conf_thresh
        assert confirm_hits >= 1
        self.confirm_hits = confirm_hits
        assert history_size >= 0
        self.history_size = history_size

        if kalman_filter_cfg is None:
            kalman_filter_cfg = SimpleNamespace()
        if flow_cfg is None:
            flow_cfg = SimpleNamespace()

        self.tracks = {}
        self.hist_tracks = OrderedDict()
        self.kf = KalmanFilter(**vars(kalman_filter_cfg))
        self.flow = Flow(self.size, **vars(flow_cfg))
        self.frame_rect = to_tlbr((0, 0, *self.size))

        self.klt_bboxes = {}
        self.homography = None

    def reset(self, dt):
        """Reset the tracker for new input context.

        Parameters
        ----------
        dt : float
            Time interval in seconds between each frame.
        """
        self.kf.reset_dt(dt)
        self.hist_tracks.clear()
        Track._count = 0

    def init(self, frame, detections):
        """Initializes the tracker from detections in the first frame.

        Parameters
        ----------
        frame : ndarray
            Initial frame.
        detections : recarray[DET_DTYPE]
            Record array of N detections.
        """
        self.tracks.clear()
        self.flow.init(frame)
        for det in detections:
            state = self.kf.create(det.tlbr)
            new_trk = Track(0, det.tlbr, det.conf, state, det.label, self.confirm_hits)
            self.tracks[new_trk.trk_id] = new_trk
            LOGGER.debug(f"{'Detected:':<14}{new_trk}")

    def track(self, frame):
        """Convenience function that combines `compute_flow` and `apply_kalman`.

        Parameters
        ----------
        frame : ndarray
            The next frame.
        """
        self.compute_flow(frame)
        self.apply_kalman()

    def compute_flow(self, frame):
        """Computes optical flow to estimate tracklet positions and camera motion.

        Parameters
        ----------
        frame : ndarray
            The next frame.
        """
        active_tracks = [track for track in self.tracks.values() if track.active]
        self.klt_bboxes, self.homography = self.flow.predict(frame, active_tracks)
        if self.homography is None:
            # clear tracks when camera motion cannot be estimated
            self.tracks.clear()

    def apply_kalman(self):
        """Performs kalman filter predict and update from KLT measurements.
        The function should be called after `compute_flow`.
        """
        for trk_id, track in list(self.tracks.items()):
            mean, cov = track.state
            mean, cov = self.kf.warp(mean, cov, self.homography)
            mean, cov = self.kf.predict(mean, cov)
            if trk_id in self.klt_bboxes:
                klt_tlbr = self.klt_bboxes[trk_id]
                # give large KLT uncertainty for occluded tracks
                # usually these with large age and low inlier ratio
                std_multiplier = max(self.age_penalty * track.age, 1) / track.inlier_ratio
                mean, cov = self.kf.update(mean, cov, klt_tlbr, MeasType.FLOW, std_multiplier)
            next_tlbr = as_tlbr(mean[:4])
            track.update(next_tlbr, (mean, cov))
            if ios(next_tlbr, self.frame_rect) < 0.5:
                if track.confirmed:
                    LOGGER.info(f"{'Out:':<14}{track}")
                self._mark_lost(trk_id)

    def update(self, frame_id, detections):
        """Associates detections to tracklets based on motion and feature embeddings.

        Parameters
        ----------
        frame_id : int
            The next frame ID.
        detections : recarray[DET_DTYPE]
            Record array of N detections.

        """
        track_th = 0.25
        det_th = 0.35

        det_high_ids = [i for i, d in enumerate(detections) if d.conf > track_th]
        det_low_ids = [i for i, d in enumerate(detections) if 0.1 < d.conf <= track_th]

        unconfirmed = [trk_id for trk_id, track in self.tracks.items() if not track.confirmed]
        confirmed = [trk_id for trk_id, track in self.tracks.items() if track.confirmed]

        # 1st association, with high score detection boxes
        h_detections = detections[det_high_ids]
        cost = self._iou_cost(confirmed, h_detections)
        matches1, u_trk_ids1, u_det_ids1 = linear_assignment(cost, confirmed, det_high_ids)

        # 2nd association, with low score detection boxes
        l_detections = detections[det_low_ids]
        cost = self._iou_cost(u_trk_ids1, l_detections)
        matches2, u_trk_ids2, u_det_ids2 = linear_assignment(cost, u_trk_ids1, det_low_ids)

        # 3rd association with unconfirmed tracks
        u_det_ids = u_det_ids1 + u_det_ids2 # itertools.chain??
        u_detections = detections[u_det_ids]
        cost = self._iou_cost(unconfirmed, u_detections)
        matches3, u_trk_ids3, u_det_ids = linear_assignment(cost, unconfirmed, u_det_ids)

        matches = itertools.chain(matches1, matches2, matches3)
        u_trk_ids = itertools.chain(u_trk_ids1, u_trk_ids2, u_trk_ids3)

        # rectify matches that may cause duplicate tracks
        matches, u_trk_ids = self._rectify_matches(matches, u_trk_ids, detections)

        # update matched tracks
        for trk_id, det_id in matches:
            track = self.tracks[trk_id]
            det = detections[det_id]
            mean, cov = self.kf.update(*track.state, det.tlbr, MeasType.DETECTOR)
            next_tlbr = as_tlbr(mean[:4])
            if track.hits == self.confirm_hits - 1:
                LOGGER.info(f"{'Found:':<14}{track}")
            if ios(next_tlbr, self.frame_rect) < 0.5: #?
                if track.confirmed:
                    LOGGER.info(f"{'Out:':<14}{track}")
                self._mark_lost(trk_id)
            track.add_detection(frame_id, next_tlbr, det.conf, (mean, cov))

        # clean up lost tracks
        for trk_id in u_trk_ids:
            track = self.tracks[trk_id]
            track.mark_missed()
            if not track.confirmed:
                LOGGER.debug(f"{'Unconfirmed:':<14}{track}")
                del self.tracks[trk_id]
                continue
            if track.age > self.max_age:
                LOGGER.info(f"{'Lost:':<14}{track}")
                self._mark_lost(trk_id)

        # u_det_ids = itertools.chain(invalid_u_det_ids, reid_u_det_ids)
        # start new tracks
        for det_id in u_det_ids:
            det = detections[det_id]
            if det.conf < det_th:
                continue
            state = self.kf.create(det.tlbr)
            new_trk = Track(frame_id, det.tlbr, det.conf, state, det.label, self.confirm_hits)
            self.tracks[new_trk.trk_id] = new_trk
            LOGGER.debug(f"{'Detected:':<14}{new_trk}")

    def _mark_lost(self, trk_id):
        track = self.tracks.pop(trk_id)
        if track.confirmed:
            self.hist_tracks[trk_id] = track
            if len(self.hist_tracks) > self.history_size:
                self.hist_tracks.popitem(last=False)

    def _group_tracks_by_depth(self, group_size=2):
        n_depth = (self.max_age + group_size) // group_size
        confirmed_by_depth = [[] for _ in range(n_depth)]
        unconfirmed = []
        for trk_id, track in self.tracks.items():
            if track.confirmed:
                depth = track.age // group_size
                confirmed_by_depth[depth].append(trk_id)
            else:
                unconfirmed.append(trk_id)
        return confirmed_by_depth, unconfirmed

    def _iou_cost(self, trk_ids, detections):
        n_trk, n_det = len(trk_ids), len(detections)
        if n_trk == 0 or n_det == 0:
            return np.empty((n_trk, n_det))

        t_labels = np.fromiter((self.tracks[trk_id].label for trk_id in trk_ids), int, n_trk)
        t_bboxes = np.array([self.tracks[trk_id].tlbr for trk_id in trk_ids])
        d_bboxes = detections.tlbr
        iou_cost = iou_dist(t_bboxes, d_bboxes)
        gate_cost(iou_cost, t_labels, detections.label, 1. - self.iou_thresh)
        return iou_cost

    def _rectify_matches(self, matches, u_trk_ids, detections):
        matches, u_trk_ids = set(matches), set(u_trk_ids)
        inactive_matches = [match for match in matches if not self.tracks[match[0]].active]
        u_active = [trk_id for trk_id in u_trk_ids
                    if self.tracks[trk_id].confirmed and self.tracks[trk_id].active]

        n_inactive_matches = len(inactive_matches)
        if n_inactive_matches == 0 or len(u_active) == 0:
            return matches, u_trk_ids

        m_inactive, det_ids = zip(*inactive_matches)
        t_bboxes = np.array([self.tracks[trk_id].tlbr for trk_id in u_active])
        d_bboxes = detections[det_ids,].tlbr
        iou_cost = iou_dist(t_bboxes, d_bboxes)

        col_indices = list(range(n_inactive_matches))
        dup_matches, _, _ = greedy_match(iou_cost, u_active, col_indices,
                                         1. - self.duplicate_thresh)

        for u_trk_id, col in dup_matches:
            m_trk_id, det_id = m_inactive[col], det_ids[col]
            t_u_active, t_m_inactive = self.tracks[u_trk_id], self.tracks[m_trk_id]
            if t_m_inactive.end_frame < t_u_active.start_frame:
                LOGGER.debug(f"{'Merged:':<14}{u_trk_id} -> {m_trk_id}")
                t_m_inactive.merge_continuation(t_u_active)
                u_trk_ids.remove(u_trk_id)
                del self.tracks[u_trk_id]
            else:
                LOGGER.debug(f"{'Duplicate:':<14}{m_trk_id} -> {u_trk_id}")
                u_trk_ids.remove(u_trk_id)
                u_trk_ids.add(m_trk_id)
                matches.remove((m_trk_id, det_id))
                matches.add((u_trk_id, det_id))
        return matches, u_trk_ids

    def _remove_duplicate(self, trk_ids1, trk_ids2):
        if len(trk_ids1) == 0 or len(trk_ids2) == 0:
            return

        bboxes1 = np.array([self.tracks[trk_id].tlbr for trk_id in trk_ids1])
        bboxes2 = np.array([self.tracks[trk_id].tlbr for trk_id in trk_ids2])

        ious = bbox_ious(bboxes1, bboxes2)
        idx = np.where(ious >= self.duplicate_thresh)
        dup_ids = set()
        for row, col in zip(*idx):
            trk_id1, trk_id2 = trk_ids1[row], trk_ids2[col]
            track1, track2 = self.tracks[trk_id1], self.tracks[trk_id2]
            if len(track1) > len(track2):
                dup_ids.add(trk_id2)
            else:
                dup_ids.add(trk_id1)
        for trk_id in dup_ids:
            LOGGER.debug(f"{'Duplicate:':<14}{self.tracks[trk_id]}")
            del self.tracks[trk_id]
