package io.github.marthofdoom.harmony

/** Compile-time feature flags. */
object Features {
    /** Bit-perfect reporting + USB-DAC mixer control is ON HOLD. Off: no badge, no
     *  "-> output" quality, no AudioManager mixer calls, no USB attach/detach logic,
     *  normal ExoPlayer volume/float handling. Flip to true to bring it all back. */
    const val BIT_PERFECT = false
}
