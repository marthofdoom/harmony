# Local playback: buffering and bit-perfect output (desktop)

**Status: built (desktop GTK app only).** "This computer" is a playback target
next to the WiiM/UPnP/Cast devices. `harmony.ui.local_player.LocalPlayer` wraps a
GStreamer `playbin` that decodes the resolved stream and hands raw audio to
`pipewiresink`. (It lives in the UI layer because GStreamer is gi; the engine
stays GTK-free. See [playback.md](playback.md) for the cast side.)

The web app and the Android app are not affected by anything on this page.

## Buffering: never let the network reach the speakers

`buffering_plan(url)` chooses, per source, before each track loads:

| Source | Plan | What happens |
|---|---|---|
| `file://` (the local Library) | none | nothing to buffer |
| `http(s)` Library / a peer's `/stream/<token>` / Qobuz FLAC | **download** | playbin's `download` flag: the whole track is fetched ahead of the play head into a temp file, as fast as the network allows. Playback starts once about two seconds of the track's own bitrate (64 KiB to 1 MiB, from the stream's quality; instant on a LAN) is ahead of it, and only holds to re-buffer if the download falls to 20 % of that window, i.e. only if the network stalls for longer than everything already fetched. |
| YouTube (`googlevideo.com`) | **stream** | a 30 s / 32 MiB look-ahead queue (sized for 24/192 FLAC), start at 15 % of it, re-buffer below 3 %. Signed, throttled URLs make a whole-file fetch unreliable. |

* The temp files live in **`<cache>/harmony/stream-buffer/dl-XXXXXX`** (inside the
  Flatpak: `~/.var/app/<id>/cache/harmony/...`), never the sandbox's RAM-backed
  `/tmp`. GStreamer deletes each at the end of its track; any a crash left are
  swept the next time the player is created.
* Seeking inside the fetched part is a plain seek. A seek *past* the download is
  never given to the download buffer: it would have to re-request the file, and if
  the server answers with 403 (an expired Qobuz URL or peer token) or ignores `Range`,
  GStreamer's flush seek never returns and freezes the main loop. The track is
  instead reloaded on the queue plan at the target (a plain Range request that fails
  fast into the usual error -> toast -> next-track path); resuming a saved position
  does the same. (`tests/test_local_seek_hang.py` reproduces the hang with a throttled
  server and checks this.) A Qobuz URL that expired is re-resolved only on a track
  change, as before; the peer's `/stream/` token is refreshed server-side.
* A source the server turns out not to serve with a length falls back to the
  look-ahead queue, with the same watermarks as the stream plan.
* The existing hold in `_handle_buffering` (pause below 100 %, resume at 100 %,
  intent never changes so the UI says "playing") is unchanged and works with both
  plans. Why a small byte window sized from the bitrate: measured, the size of the
  buffer in *bytes* decides when playback starts (a 4 MiB window cost 4.3 s on a
  1 MB/s link and 6 s for a small MP3 at 100 KB/s, against 0.5 s / 0.09 s before; the
  time cap is only used once GStreamer knows a bitrate, which a download buffer never
  does). The resilience comes from the download running ahead behind the small window.
* `pipewiresink` gets `node.latency=2048/48000` (~43 ms; the graph default is 21 ms)
  and a 100 ms `processing-deadline`. Measured against a private PipeWire with all
  8 cores busy for 20 s: unset or 1024 gave 4 xruns, 2048 and 4096 gave 2, and the
  sink-monitor capture had no silent gap in any run -- so 2048, the smaller of the two
  that help. The graph runs at the smallest latency any node requests, so this only
  matters when Harmony is the only requester (then other apps share a 43 ms quantum
  instead of 21 ms).
* Not done: pre-fetching the **next** track before the current one ends. The queue
  advance is an async resolve + `load_and_play`; true gapless/prefetch would need a
  second pipeline or `about-to-finish` URI swapping, which would bypass that logic.

`scripts/e2e_buffering.py` is the real-path check: a throttled Range server that
stalls once for 25 s mid-file, played through the real `LocalPlayer` into a private
PipeWire (it also runs inside the Flatpak sandbox). With the stock playbin
buffering the audio is held for 4-7 s (a 4.84 s run of digital silence in the
sink-monitor capture); with the download plan the monitor capture shows no silent
gap in 12 of 12 runs, first audio at ~1.6 s, and the temp file is created under
the cache dir and removed afterwards. (In some runs without the recorder attached the
*play-head readout* froze for 0.2-0.7 s with no buffering message, no PipeWire xrun
and no dropped buffer; it did not reproduce under capture and is not known to be
audible.)

## Bit-perfect output

One switch -- **Preferences > Audio > Bit-perfect output**, or the pill beside the
quality label on the Now Playing bar and page -- plus an indicator that is green
**only when the output is verified unmodified**.

### What the switch does

* Software volume is fixed at 100 % for "This computer": the bar's slider is
  disabled ("Volume is controlled by your DAC/amp"), and `AppState.playback_set_volume`
  ignores requests, so MPRIS `Volume` and media keys cannot change it either. The
  level you had is remembered and restored when you turn it off.
* The PipeWire stream is opened at the track's own rate and format (44.1, 48, 88.2,
  96, 176.4, 192 kHz; S16/S24/S32 as decoded). `pipewiresink` stream properties:
  `node.dont-remix=true` (never mix or up-mix channels) and `node.lock-rate=true`
  (another app starting later cannot pull the output to its own rate). The lock is
  only held while the stream is *running*: measured, once Harmony is paused (the node
  goes idle) another app can take the output at its own rate at once, and on resume
  Harmony is the one resampled -- which the indicator then reports ("Firefox is
  playing at 48 kHz and holds the output's rate"). Nothing needs releasing.
* Resampling is deliberately left *allowed*: a track the device cannot run natively
  still plays (resampled by PipeWire) and the indicator says so, rather than
  failing. The rate is **never forced** past PipeWire's `clock.allowed-rates`.
* No dither (with bit-perfect on the in-pipeline `audioconvert` is pinned to
  `dithering=none`; with it off GStreamer's default TPDF dither is left alone), no
  ReplayGain, no normalisation: the local pipeline contains none.
* Turning it on or off mid-track reloads the current track at the same position
  (the PipeWire stream properties are fixed when the stream connects): it goes through
  the normal play path, so the stream URL is re-resolved rather than reusing an
  expired one. The pill on the bar turns it **off** in one click, but only opens
  Preferences > Audio to turn it **on** (it pins the volume to 100%, a big jump from a
  quiet setting).

### What the indicator checks (`harmony.audio.bitperfect.evaluate`)

Grey = off, or on but not measurable yet. **Green only when all of these hold**, read
from the live PipeWire graph (`pw-dump`, which ships with PipeWire and in the
Flatpak runtime -- no extra permission) rather than from GStreamer's own caps:

1. the app volume is 100 % (and not muted);
2. nothing converted the audio inside Harmony (decoded caps == caps at the sink,
   a repack such as `S24_32LE`<->`S24LE` aside);
3. the stream opened at the track's rate and the **output node runs at that rate**
   (`Format` of the sink node);
4. no channel mixing, and the output carries every bit (a float graph holds 24-bit PCM);
5. PipeWire applies no software gain (stream volume, the sink's software volume --
   a hardware mixer is not processing and is ignored);
6. no other app is mixed into the same output;
7. the output is a **real device** (ALSA). A Bluetooth output is amber "Re-encoded by
   Bluetooth"; a virtual one (EasyEffects, a filter-chain, loopback, combine, a null
   sink) is amber "Can't verify past <name>", since whatever it does downstream
   cannot be seen. Every output the stream feeds is considered (a recorder such as
   OBS attached to the app's audio is ignored), preferring the real device.

Otherwise amber, naming the **first** failing item, with the reason and fix in the
tooltip: `Resampled 96→48 kHz` ("The output doesn't allow 96 kHz ..."),
`Volume below 100%`, `Mixed with other audio` (names the app), `Bit depth 24→16`,
`Can't verify` (no `pw-dump`/daemon). A **lossy source** passes the path checks and
is then amber as `Lossy source (AAC)` -- *decoded unmodified*, but a lossy stream
cannot be bit-perfect to the recording.

The probe runs off the main thread when a track starts and every ~5 s while playing,
re-reading every second only while the stream is still connecting (it backs off to 5 s
if it never links), and is discarded when the track changes.

### Letting the output follow the track's rate (PipeWire)

PipeWire runs the whole graph at one rate and only switches it to match a stream
when that rate is in `default.clock.allowed-rates` (the default is just `[ 48000 ]`,
so every 44.1 / 96 / 192 kHz track is resampled). Harmony, especially as a Flatpak,
does not edit your PipeWire configuration. To allow the usual rates, run once:

```sh
mkdir -p ~/.config/pipewire/pipewire.conf.d && cat > ~/.config/pipewire/pipewire.conf.d/10-bit-perfect.conf <<'EOF'
context.properties = {
    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 ]
}
EOF
systemctl --user restart pipewire pipewire-pulse wireplumber
```

(Preferences > Audio shows the same command with a copy button.) Check with
`pw-metadata -n settings 0 clock.allowed-rates`. The DAC itself must support the
rates; a device that does not will be resampled by its own driver, which Harmony
cannot see.

### Limits (stated plainly)

* PipeWire processes in float32: 16- and 24-bit PCM pass through losslessly (24-bit
  fits a float's mantissa; verified sample-exact below), true 32-bit integer audio
  would not. FLAC tops out at 24-bit.
* This is not ALSA `hw:` exclusive mode. Another app that plays at the same time is
  mixed (the indicator says so); `node.lock-rate` only stops it changing the rate.
* The DAC's own clock, jitter and USB transport are hardware, not ours. Bluetooth is
  re-encoded to the link codec and is never bit-perfect; casting to a WiiM/Chromecast
  is passthrough to a device we do not control, so the indicator is shown for "This
  computer" only.

### How it was verified

`scripts/e2e_bit_perfect.py` drives the real `AppState` + `LocalPlayer` +
`NowPlayingBar` against a **private** PipeWire daemon (own socket, a null sink, a
real WirePlumber; the system graph and config are untouched). For 44.1/16, 48/24,
96/24 and 192/24 FLACs it reads `pw-dump` independently of Harmony and checks the
output node's rate, the indicator and the pill; against a daemon limited to
`[ 48000 ]` it checks the amber states and tooltips; and it records the sink's
monitor and compares it **sample for sample** with the decoded file (identical at
every rate; a 50 % volume control differs everywhere). Recipe for the daemon:

```sh
D=~/.cache/harmony-e2e/pw; mkdir -p $D/run
sed 's|^\s*#default.clock.allowed-rates.*|    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 ]|' \
    /usr/share/pipewire/pipewire.conf > $D/pipewire.conf
cat >> $D/pipewire.conf <<'EOF'
context.objects = [ { factory = adapter args = { factory.name = support.null-audio-sink
    node.name = bp-test-sink media.class = Audio/Sink audio.position = [ FL FR ]
    node.driver = true object.linger = true } } ]
EOF
PIPEWIRE_RUNTIME_DIR=$D/run PIPEWIRE_CORE=pw-test pipewire -c $D/pipewire.conf &
mkdir -p $D/wp/wireplumber/wireplumber.conf.d      # no real devices in the throw-away graph
printf 'wireplumber.profiles = { main = { monitor.alsa = disabled monitor.alsa-midi = disabled monitor.bluez = disabled monitor.bluez-midi = disabled monitor.v4l2 = disabled monitor.libcamera = disabled } }\n' \
    > $D/wp/wireplumber/wireplumber.conf.d/10-test.conf
mkdir -p $D/state    # a throw-away WirePlumber must never write ~/.local/state/wireplumber
PIPEWIRE_RUNTIME_DIR=$D/run PIPEWIRE_REMOTE=pw-test XDG_CONFIG_HOME=$D/wp XDG_STATE_HOME=$D/state wireplumber &
# the null sink is virtual (never verified green by the app); the script marks it a
# real device through the test-only HARMONY_BITPERFECT_TEST_SINKS
PIPEWIRE_RUNTIME_DIR=$D/run PIPEWIRE_REMOTE=pw-test E2E_SINK=bp-test-sink \
    ./.venv/bin/python scripts/e2e_bit_perfect.py --capture
```
