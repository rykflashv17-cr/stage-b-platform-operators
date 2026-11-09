"""Measurement facts shared by statistical admission and quality rules."""
import numpy as np


def coverage_policy(config):
    return (config.left_topic, config.right_topic, config.max_nearest_gap_ns) if config else None


def measurement_distances(episode, config):
    """Return nearest valid reading distances; apply no threshold or verdict."""
    result = {}
    for side, topic in (("left", config.left_topic), ("right", config.right_topic)):
        if topic not in episode.valid_measurement_times_ns:
            raise ValueError(f"adapter did not provide valid measurement timestamps for: {topic}")
        observed = episode.valid_measurement_times_ns[topic]
        if not len(observed):
            distances = None
        else:
            right = np.clip(np.searchsorted(observed, episode.frame_times_ns), 0, len(observed) - 1)
            left = np.maximum(right - 1, 0)
            distances = np.minimum(np.abs(episode.frame_times_ns - observed[left]),
                                   np.abs(episode.frame_times_ns - observed[right]))
        result[side] = (topic, distances)
    return result
