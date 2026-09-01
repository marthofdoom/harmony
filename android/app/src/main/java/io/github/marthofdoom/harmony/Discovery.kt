package io.github.marthofdoom.harmony

import android.content.Context
import android.net.nsd.NsdManager
import android.net.nsd.NsdServiceInfo
import android.util.Log
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

private const val SERVICE_TYPE = "_harmony._tcp."
private const val TAG = "HarmonyDiscovery"

/** Discovers Harmony instances on the LAN via mDNS (Network Service Discovery) —
 *  the light-client half of the mesh: a phone finds an instance to present its
 *  personal key to. */
class Discovery(context: Context) {
    private val nsd = context.getSystemService(Context.NSD_SERVICE) as NsdManager
    private val _instances = MutableStateFlow<List<Instance>>(emptyList())
    val instances: StateFlow<List<Instance>> = _instances

    private var discoveryListener: NsdManager.DiscoveryListener? = null
    private var registrationListener: NsdManager.RegistrationListener? = null

    /** Advertise this phone as a Harmony instance on [port] so the desktop and
     *  server see it on the mesh (Route Audio, device lists) — the missing half
     *  that made the phone invisible. [name] is the advertised service name. */
    fun advertise(port: Int, name: String) {
        if (registrationListener != null || port <= 0) return
        val info = NsdServiceInfo().apply {
            serviceName = name
            serviceType = SERVICE_TYPE
            setPort(port)
        }
        val listener = object : NsdManager.RegistrationListener {
            override fun onServiceRegistered(i: NsdServiceInfo) { Log.i(TAG, "advertising ${i.serviceName}") }
            override fun onRegistrationFailed(i: NsdServiceInfo, code: Int) { Log.w(TAG, "register failed $code") }
            override fun onServiceUnregistered(i: NsdServiceInfo) {}
            override fun onUnregistrationFailed(i: NsdServiceInfo, code: Int) {}
        }
        registrationListener = listener
        runCatching { nsd.registerService(info, NsdManager.PROTOCOL_DNS_SD, listener) }
            .onFailure { Log.w(TAG, "registerService failed", it) }
    }

    fun start() {
        if (discoveryListener != null) return
        val listener = object : NsdManager.DiscoveryListener {
            override fun onServiceFound(info: NsdServiceInfo) = resolve(info)
            override fun onServiceLost(info: NsdServiceInfo) {
                _instances.value = _instances.value.filterNot { it.name == info.serviceName }
            }
            override fun onDiscoveryStarted(t: String) {}
            override fun onDiscoveryStopped(t: String) {}
            override fun onStartDiscoveryFailed(t: String, code: Int) { Log.w(TAG, "start failed $code") }
            override fun onStopDiscoveryFailed(t: String, code: Int) {}
        }
        discoveryListener = listener
        runCatching { nsd.discoverServices(SERVICE_TYPE, NsdManager.PROTOCOL_DNS_SD, listener) }
            .onFailure { Log.w(TAG, "discoverServices failed", it) }
    }

    private fun resolve(info: NsdServiceInfo) {
        // A fresh listener per resolve avoids the "listener already in use" error
        // on older Android when several services resolve at once.
        val listener = object : NsdManager.ResolveListener {
            override fun onServiceResolved(resolved: NsdServiceInfo) {
                val host = resolved.host?.hostAddress ?: return
                val inst = Instance(resolved.serviceName ?: host, host, resolved.port)
                _instances.value = (_instances.value.filterNot { it.name == inst.name } + inst)
                    .sortedBy { it.name }
            }
            override fun onResolveFailed(i: NsdServiceInfo, code: Int) { Log.d(TAG, "resolve failed $code") }
        }
        runCatching { nsd.resolveService(info, listener) }
    }

    fun stop() {
        discoveryListener?.let { runCatching { nsd.stopServiceDiscovery(it) } }
        discoveryListener = null
        registrationListener?.let { runCatching { nsd.unregisterService(it) } }
        registrationListener = null
        _instances.value = emptyList()
    }
}
