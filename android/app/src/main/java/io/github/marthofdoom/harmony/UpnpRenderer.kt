package io.github.marthofdoom.harmony

import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.URI
import java.util.concurrent.TimeUnit

private const val TAG = "HarmonyUpnp"

/** A discovered UPnP/DLNA MediaRenderer on the phone's local network (a WiiM,
 *  a TV, a networked speaker). [host] is its LAN address; [controlUrl] is its
 *  AVTransport control endpoint. */
data class UpnpRenderer(val name: String, val host: String, val controlUrl: String)

/** SSDP discovery + AVTransport (SetAVTransportURI/Play/Stop) control, hand-rolled
 *  so we pull in no UPnP library. Used by the phone-bridge: the phone points a
 *  local renderer at its own relay URL (see LocalRelay), so a track from a
 *  VPN-remote hub still plays on a device on the phone's LAN. */
object Upnp {
    private val http = OkHttpClient.Builder()
        .connectTimeout(5, TimeUnit.SECONDS).readTimeout(8, TimeUnit.SECONDS).build()

    private const val SSDP_ADDR = "239.255.255.250"
    private const val SSDP_PORT = 1900
    private const val TARGET = "urn:schemas-upnp-org:device:MediaRenderer:1"

    /** Blocking SSDP M-SEARCH; returns MediaRenderers found within ~3s. Call
     *  from a background dispatcher, holding a WifiManager MulticastLock. */
    fun discover(): List<UpnpRenderer> {
        val locations = linkedSetOf<String>()
        val msearch = (
            "M-SEARCH * HTTP/1.1\r\n" +
            "HOST: $SSDP_ADDR:$SSDP_PORT\r\n" +
            "MAN: \"ssdp:discover\"\r\n" +
            "MX: 2\r\n" +
            "ST: $TARGET\r\n\r\n"
        ).toByteArray()
        runCatching {
            DatagramSocket().use { sock ->
                sock.reuseAddress = true
                sock.soTimeout = 3000
                sock.send(DatagramPacket(msearch, msearch.size,
                    InetSocketAddress(InetAddress.getByName(SSDP_ADDR), SSDP_PORT)))
                val buf = ByteArray(2048)
                val deadline = System.nanoTime() + 3_000_000_000L
                while (System.nanoTime() < deadline) {
                    val pkt = DatagramPacket(buf, buf.size)
                    try {
                        sock.receive(pkt)
                    } catch (_: java.net.SocketTimeoutException) {
                        break
                    }
                    val text = String(pkt.data, 0, pkt.length)
                    locationOf(text)?.let { locations.add(it) }
                }
            }
        }.onFailure { Log.w(TAG, "ssdp failed", it) }
        return locations.mapNotNull { describe(it) }
    }

    private fun locationOf(response: String): String? =
        response.lineSequence().firstOrNull { it.startsWith("LOCATION:", true) }
            ?.substringAfter(':', "")?.trim()?.takeIf { it.startsWith("http") }

    /** Fetch a device description XML and pull out its name + AVTransport control URL. */
    private fun describe(location: String): UpnpRenderer? = runCatching {
        val xml = http.newCall(Request.Builder().url(location).build()).execute().use {
            it.body?.string().orEmpty()
        }
        val name = Regex("<friendlyName>(.*?)</friendlyName>", RegexOption.DOT_MATCHES_ALL)
            .find(xml)?.groupValues?.get(1)?.trim() ?: URI(location).host
        // Find the <service> block whose serviceType is AVTransport, take its controlURL.
        val control = Regex("<service>(.*?)</service>", RegexOption.DOT_MATCHES_ALL)
            .findAll(xml).map { it.groupValues[1] }
            .firstOrNull { it.contains("AVTransport", true) }
            ?.let { Regex("<controlURL>(.*?)</controlURL>").find(it)?.groupValues?.get(1)?.trim() }
            ?: return null
        val base = URI(location)
        val controlUrl = if (control.startsWith("http")) control else base.resolve(control).toString()
        UpnpRenderer(name, base.host, controlUrl)
    }.getOrNull()

    fun setUriAndPlay(renderer: UpnpRenderer, uri: String): Boolean {
        val ok = soap(renderer.controlUrl, "SetAVTransportURI",
            "<InstanceID>0</InstanceID>" +
            "<CurrentURI>${xmlEscape(uri)}</CurrentURI>" +
            "<CurrentURIMetaData></CurrentURIMetaData>")
        if (!ok) return false
        return soap(renderer.controlUrl, "Play", "<InstanceID>0</InstanceID><Speed>1</Speed>")
    }

    fun stop(renderer: UpnpRenderer): Boolean =
        soap(renderer.controlUrl, "Stop", "<InstanceID>0</InstanceID>")

    private fun soap(controlUrl: String, action: String, args: String): Boolean {
        val service = "urn:schemas-upnp-org:service:AVTransport:1"
        val body =
            "<?xml version=\"1.0\"?>" +
            "<s:Envelope xmlns:s=\"http://schemas.xmlsoap.org/soap/envelope/\" " +
            "s:encodingStyle=\"http://schemas.xmlsoap.org/soap/encoding/\"><s:Body>" +
            "<u:$action xmlns:u=\"$service\">$args</u:$action>" +
            "</s:Body></s:Envelope>"
        val req = Request.Builder().url(controlUrl)
            .header("SOAPACTION", "\"$service#$action\"")
            .post(body.toRequestBody("text/xml; charset=\"utf-8\"".toMediaType()))
            .build()
        return runCatching {
            http.newCall(req).execute().use { it.isSuccessful }
        }.onFailure { Log.w(TAG, "$action failed", it) }.getOrDefault(false)
    }

    private fun xmlEscape(s: String) =
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
}
