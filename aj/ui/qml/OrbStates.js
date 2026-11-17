.pragma library

// What the orb does in each status, as target values. The orb springs from wherever it is to
// these, so a status change mid-animation morphs instead of jumping. Rates are per second:
// spin in rad, flow is the firing clock.

// The defaults are listening's: when nobody is talking the orb doesn't turn; only a spark
// lights a neuron now and then. Speaking adds turning and firing in proportion to the voice
// (Orb.qml), so in a silence it is exactly this still.
var defaults = {
    shell: 1.0, dim: 0.85, dotScale: 1.0, fire: 0.0, spark: 0.0012, linkAlpha: 0.20,
    spinRate: 0.0, flowRate: 0.25, coreGlow: 0.35, coreRate: 0.0, ringAlpha: 0.22,
    ringTight: 0.0, ringRate: 0.0, glow: 0.8, assemble: 1.0,
    wSpeak: 0, wHear: 0, wDictate: 0, wWork: 0, wLoad: 0, wFail: 0, wError: 0, wSleep: 0
};

var looks = {
    // Dots come out of the core and lock into place while a scan sweeps over them.
    "loading": { wLoad: 1, ringAlpha: 0.4, spinRate: 0.3, flowRate: 0.5, spark: 0.01, coreGlow: 0.6, dim: 0.8 },
    // Awake and idle: still, but for a spark now and then.
    "listening": {},
    // The side facing you leans in; your voice ripples across it. Still when you're quiet.
    "hearing": { wHear: 1, coreGlow: 0.55, dim: 0.95, linkAlpha: 0.24 },
    // Neurons firing: cascades, pulses on the synapses, the core spinning bright.
    "thinking": { fire: 1, spark: 0.012, flowRate: 1.0, spinRate: 0.45, coreGlow: 1.0, coreRate: 1.6,
                  linkAlpha: 0.28, dim: 1.0, glow: 1.1, ringAlpha: 0.3, ringRate: 0.3 },
    // A tool is running: dot-matrix bands scroll, gyroscope rings lock round the core.
    "working": { wWork: 1, ringAlpha: 0.95, ringTight: 1, ringRate: 2.2, flowRate: 0.6, spinRate: 0.15,
                 coreGlow: 0.8, coreRate: 1.0, fire: 0.25, dim: 1.0 },
    // Moves with AJ's voice, and only with it (the turning and firing come from the voice).
    "speaking": { wSpeak: 1, coreGlow: 0.7, dim: 1.0, linkAlpha: 0.24 },
    // A waveform round the middle follows your voice; the core blinks like a text cursor.
    "dictating": { wDictate: 1, ringAlpha: 0.1, coreGlow: 0.7 },
    // Everything off: small, dim, no rings, the core dark.
    "mic off": { shell: 0.9, dim: 0.32, spark: 0, flowRate: 0, coreGlow: 0.05, ringAlpha: 0,
                 linkAlpha: 0.06, glow: 0.3 },
    // Orange: rings of it pulse out of the core, one a second; the failing side sags and glows.
    "mic failed": { wFail: 1, dim: 0.75, spark: 0, coreGlow: 0.85, ringAlpha: 0.08, glow: 1.0 },
    // A dark lattice round a softly breathing core.
    "asleep": { wSleep: 1, dim: 0.28, spark: 0.0003, flowRate: 0.1, coreGlow: 0.45, ringAlpha: 0,
                linkAlpha: 0.08, glow: 0.5 },
    // Asleep and not listening at all (on battery, or with the mic button off): darker still, and
    // the core barely breathes, as nothing said can wake it.
    "asleep on battery": { wSleep: 0.35, dim: 0.22, spark: 0, flowRate: 0, coreGlow: 0.22, ringAlpha: 0,
                           linkAlpha: 0.05, glow: 0.35 },
    "asleep, mic off": { wSleep: 0.35, shell: 0.9, dim: 0.2, spark: 0, flowRate: 0, coreGlow: 0.18,
                         ringAlpha: 0, linkAlpha: 0.05, glow: 0.3 },
    // Back into the core: the way it came.
    "turning off": { assemble: 0, dim: 0, coreGlow: 0, ringAlpha: 0, spinRate: 0.3, fire: 0, spark: 0 },
    // Bright red, throbbing slowly: something's wrong. Frozen and drifted apart, with gaps.
    "error": { wError: 1, dim: 0.95, flowRate: 0, spark: 0, coreGlow: 1.1, ringAlpha: 0.2, linkAlpha: 0.22, glow: 1.2 }
};

var labels = {
    "loading": "Loading…",
    "listening": "Listening",
    "hearing": "Hearing you",
    "thinking": "Thinking",
    "working": "Working · weather",
    "speaking": "Speaking",
    "dictating": "Listening for dictation",
    "mic off": "Mic off",
    "mic failed": "Microphone not working",
    "asleep": "Asleep · say its name to wake it",
    "asleep on battery": "Asleep · not listening on battery",
    "asleep, mic off": "Asleep · mic off",
    "turning off": "Turning off…",
    "error": "Stopped: something went wrong"
};

var order = ["loading", "listening", "hearing", "thinking", "working", "speaking", "dictating",
             "mic off", "mic failed", "asleep", "asleep on battery", "asleep, mic off", "turning off", "error"];

function targets(status) {
    var out = {};
    var k;
    for (k in defaults)
        out[k] = defaults[k];
    var look = looks[status] || {};
    for (k in look)
        out[k] = look[k];
    return out;
}
