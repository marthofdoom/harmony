package io.github.marthofdoom.harmony

import android.util.Log
import java.io.BufferedReader
import java.io.InputStreamReader
import java.net.ServerSocket
import java.net.Socket
import kotlin.concurrent.thread

/**
 * A tiny always-on HTTP presence so this phone is a first-class Harmony instance
 * on the LAN mesh — the desktop and server can see it (Route Audio, device
 * lists) instead of it being an invisible client. Answers the handshake
 * endpoints other instances probe:
 *
 *   GET /healthz       -> {"status":"ok","service":"harmony","version":..,"name":..}
 *   GET /api/devices   -> {"devices":[]}     (the phone exposes none yet)
 *   GET /api/instances -> {"instances":[]}
 *
 * It deliberately serves no credentials or control endpoints — the phone is a
 * light client. Pair with [Discovery.advertise] to register it over NSD.
 */
class InstanceServer(private val name: String, private val version: String) {
    @Volatile private var running = false
    private var server: ServerSocket? = null

    /** Binds an ephemeral port and starts serving; returns the port. */
    fun start(): Int {
        if (running) return server?.localPort ?: 0
        val sock = ServerSocket(0)
        server = sock
        running = true
        thread(name = "harmony-instance", isDaemon = true) { acceptLoop(sock) }
        Log.i(TAG, "instance server on :${sock.localPort}")
        return sock.localPort
    }

    fun stop() {
        running = false
        runCatching { server?.close() }
    }

    private fun acceptLoop(sock: ServerSocket) {
        while (running) {
            val client = try { sock.accept() } catch (_: Exception) { break }
            thread(isDaemon = true) { runCatching { handle(client) } }
        }
    }

    private fun handle(client: Socket) {
        client.use { c ->
            val reader = BufferedReader(InputStreamReader(c.getInputStream()))
            val requestLine = reader.readLine() ?: return
            while (true) {
                val line = reader.readLine() ?: break
                if (line.isEmpty()) break
            }
            val path = requestLine.split(" ").getOrNull(1) ?: "/"
            val body = when {
                path.startsWith("/healthz") ->
                    """{"status":"ok","service":"harmony","version":"$version","name":"${esc(name)}"}"""
                path.startsWith("/api/devices") -> """{"devices":[]}"""
                path.startsWith("/api/instances") -> """{"instances":[]}"""
                else -> null
            }
            val out = c.getOutputStream()
            if (body == null) {
                out.write("HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".toByteArray())
            } else {
                val bytes = body.toByteArray()
                out.write(
                    ("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n" +
                        "Content-Length: ${bytes.size}\r\nConnection: close\r\n\r\n").toByteArray()
                )
                out.write(bytes)
            }
            out.flush()
        }
    }

    private fun esc(s: String) = s.replace("\\", "\\\\").replace("\"", "\\\"")

    companion object { private const val TAG = "HarmonyInstance" }
}
