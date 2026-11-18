"""Measurement facts shared by statistical admission and quality rules."""
import numpy as np


def coverage_policy(config):
    return (config.left_topic, config.right_topic, config.max_nearest_gap_ns) if config else None


def joint_target_policy(config):
    return (config.topic, config.max_nearest_gap_ns) if config else None


def _nearest_distances(reference, observed):
    """Use source integer nanoseconds; an empty fact array means no readings."""
    if not len(observed):
        return None
    right = np.clip(np.searchsorted(observed, reference), 0, len(observed) - 1)
    left = np.maximum(right - 1, 0)
    return np.minimum(np.abs(reference - observed[left]), np.abs(reference - observed[right]))


def _topic_distances(episode, topic):
    if topic not in episode.valid_measurement_times_ns:
        raise ValueError(f"adapter did not provide valid measurement timestamps for: {topic}")
    return _nearest_distances(episode.frame_times_ns, episode.valid_measurement_times_ns[topic])


def joint_target_distances(episode, config):
    """Return target freshness facts, never treating absent metadata as valid."""
    return _topic_distances(episode, config.topic)


def measurement_distances(episode, config):
    """Return nearest valid reading distances; apply no threshold or verdict."""
    result = {}
    for side, topic in (("left", config.left_topic), ("right", config.right_topic)):
        result[side] = (topic, _topic_distances(episode, topic))
    return result
