    // Shared by background.frag and glass.frag (QML binds both to the same Liquid and Theme
    // values), so the glass can redraw exactly the background that is behind it.
    vec2 win;      // window size, logical px
    float t;       // slow clock for the background's drift, s
    vec2 ptr;      // the pointer, window px
    vec2 vel;      // its smoothed velocity, px/s
    float smear;   // how far the pointer drags the background along
    float shine;   // light catching the warped background
    float grid;    // the faint dot grid the warp bends
    float warmth;  // how much ember colour the background has
    vec2 orbC;     // the orb's centre, window px
    float orbR;    // its radius, px
    float orbE;    // the light it casts, 0..1
    vec3 orbTint;  // and its colour: warm white, red in error, orange when the mic has failed
