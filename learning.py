"""Small side-specific online logistic corrections learned from realized net outcomes."""
import numpy as np

INPUTS = ("bias", "base_conviction", "spread_fraction", "quote_impulse", "recent_imbalance")
RATE = 0.035


def blank():
    return {side: {"weights": [0.] * len(INPUTS), "closed": 0} for side in ("BUY", "SELL")}


def combined(demo, online):
    """Combine independently learned demo-trade and public-data adjustments."""
    result = blank()
    for side in ("BUY", "SELL"):
        result[side]["weights"] = (np.asarray(demo[side]["weights"], dtype=float)
                                   + np.asarray(online[side]["weights"], dtype=float)).tolist()
        result[side]["closed"] = int(demo[side]["closed"]) + int(online[side]["closed"])
    return result


def clip_logit(p):
    p = np.clip(p, .001, .999)
    return np.log(p/(1-p))


def context(base, side, spread_fraction, impulse, imbalance):
    return np.array([1., np.clip(clip_logit(base), -2., 2.), np.clip(spread_fraction*4, 0., 2.),
                     np.clip(side*impulse, -2., 2.), np.clip(side*imbalance, -1., 1.)])


def adjusted(state, base, inputs):
    offset = np.clip(np.dot(state["weights"], inputs), -.65, .65)
    return float(1 / (1 + np.exp(-(clip_logit(base) + offset))))


def update(state, base, inputs, net):
    before = adjusted(state, base, inputs)
    weights = np.array(state["weights"]) * .998 + RATE * (int(net > 0) - before) * np.asarray(inputs)
    state["weights"] = np.clip(weights, -.30, .30).tolist()
    state["closed"] += 1
    return before, adjusted(state, base, inputs)


def side_scores(classes, probabilities, states, context_values, threshold):
    raw = dict(zip(classes, probabilities))
    outputs, vectors = {}, {}
    spread_fraction, impulse, imbalance = context_values
    for side, label in ((1, "BUY"), (-1, "SELL")):
        base = float(raw.get(side, .001))
        vector = context(base, side, spread_fraction, impulse, imbalance)
        vectors[side] = vector
        outputs[side] = adjusted(states[label], base, vector)
    winner = max(outputs, key=outputs.get)
    if outputs[winner] < threshold or outputs[winner] <= raw.get(0, 0):
        winner = 0
    return winner, outputs, vectors
