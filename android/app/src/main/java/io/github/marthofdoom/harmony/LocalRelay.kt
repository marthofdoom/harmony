package io.github.marthofdoom.harmony

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.TimeUnit
import kotlin.concurrent.thread

private const val TAG = "HarmonyRelay"

/** A minimal HTTP relay the phone runs so a UPnP renderer on the phone's LAN can
 *  fetch a stream that actually lives on the (possibly VPN-remote) hub. The
 *  renderer GETs the phone; the phone proxies to the hub's `/stream` URL,
 *  forwarding Range so seeking works. This is the phone-bridge data plane. */
class LocalRelay {
    @Volatile private var upstream: String? = null
    @Volatile private var running = false
    private var server: ServerSocket? = null
    private val http = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS).readTimeout(30, TimeUnit.SECONDS).build()

    /** Starts the relay for [upstreamUrl] and returns the port it bound. */
    fun start(upstreamUrl: String): Int {
        upstream = upstreamUrl
        if (running) return server?.localPort ?: 0
        val sock = ServerSocket(0)
        server = sock
        running = true
        thread(name = "harmony-relay", isDaemon = true) { acceptLoop(sock) }
        return sock.localPort
    }

    fun setUpstream(upstreamUrl: String) { upstream = upstreamUrl }

    private fun acceptLoop(sock: ServerSocket) {
        while (running) {
            val client = try { sock.accept() } catch (_: Exception) { break }
            thread(isDaemon = true) { runCatching { handle(client) }.onFailure { client.closeQuietly() } }
        }
    }

    private fun handle(client: Socket) {
        client.use { c ->
            val reader = BufferedReader(InputStreamReader(c.getInputStream()))
            val requestLine = reader.readLine() ?: return
            val isHead = requestLine.startsWith("HEAD")
            var range: String? = null
            while (true) {
                val line = reader.readLine() ?: break
                if (line.isEmpty()) break
                if (line.startsWith("Range:", true)) range = line.substringAfter(':').trim()
            }
            val target = upstream ?: run { writeStatus(c, 503, "no upstream"); return }

            val reqB = Request.Builder().url(target)
            if (range != null) reqB.header("Range", range)
            if (isHead) reqB.head()
            http.newCall(reqB.build()).execute().use { up ->
                val out = c.getOutputStream()
                val code = up.code
                val reason = if (code == 206) "Partial Content" else if (code == 200) "OK" else "Error"
                val sb = StringBuilder("HTTP/1.1 $code $reason\r\n")
                up.header("Content-Type")?.let { sb.append("Content-Type: $it\r\n") }
                up.header("Content-Length")?.let { sb.append("Content-Length: $it\r\n") }
                up.header("Content-Range")?.let { sb.append("Content-Range: $it\r\n") }
                sb.append("Accept-Ranges: bytes\r\n")
                // DLNA renderers key off this to start streaming playback.
                sb.append("transferMode.dlna.org: Streaming\r\n")
                sb.append("Connection: close\r\n\r\n")
                out.write(sb.toString().toByteArray())
                if (!isHead) {
                    up.body?.byteStream()?.use { it.copyTo(out, 64 * 1024) }
                }
                out.flush()
            }
        }
    }

    private fun writeStatus(c: Socket, code: Int, msg: String) {
        runCatching { c.getOutputStream().write("HTTP/1.1 $code $msg\r\nConnection: close\r\n\r\n".toByteArray()) }
    }

    fun stop() {
        running = false
        runCatching { server?.close() }
        server = null
        upstream = null
    }
}

private fun Socket.closeQuietly() { runCatching { close() } }
