package io.github.marthofdoom.harmony

import android.content.Context

/** Remembers the last-connected instance and personal key so the app reconnects
 *  on launch without rediscovery. */
class Prefs(context: Context) {
    private val sp = context.getSharedPreferences("harmony", Context.MODE_PRIVATE)

    var baseUrl: String?
        get() = sp.getString("base_url", null)
        set(v) = sp.edit().putString("base_url", v).apply()

    var key: String?
        get() = sp.getString("key", null)
        set(v) = sp.edit().putString("key", v).apply()
}
