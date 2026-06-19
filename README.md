# hamlib-tci-sidecar

Bridges between Hamlib's TCI 2.0 driver and ham-radio software, providing **full audio (bidirectional RX/TX)** and **IQ (RX-only)** streaming.

Three sidecar processes ship in this repo:

- **`tci-audio-soundcard-sidecar.py`** — audio bridge for soundcard-
  style consumers.  RX/TX audio as PulseAudio null sinks for JS8Call,
  fldigi, WSJT-X, and anything else that wants to plug into a virtual
  soundcard.  Bidirectional.  Linux today (PulseAudio / PipeWire);
  Windows and macOS implementations under the same name will follow.
- **`tci-audio-gr-sidecar.py`** — audio bridge for GNU Radio.  RX
  audio published as ZMQ PUB (float32 mono 8 kHz); TX audio accepted
  on a ZMQ PULL socket.  Bidirectional.  An alternative to
  `tci-audio-soundcard-sidecar.py` — pick whichever consumer style
  fits.
- **`tci-iq-sidecar.py`** — IQ bridge.  Receiver IQ stream for GNU
  Radio and other ZMQ-aware consumers.  **RX-only — TCI 2.0 does not
  define a TX IQ stream.**  Independent of the audio sidecars; both can
  run together.

Plus two GR-side tools:

- **`tci-iq-viewer.py`** — Qt FFT + waterfall against the IQ sidecar,
  with click-to-tune.
- **`tci-audio-gr-tester.py`** — Qt FFT + waveform of RX audio plus
  a 1 kHz tone generator gated by a PTT button.  Demonstrates the
  full GR audio integration.

> **Status: Linux full support; Windows/macOS partial.**  
> - **Linux:** All sidecars working (PulseAudio/PipeWire audio, ZMQ-based GR audio, IQ).
> - **Windows/macOS:** GR audio sidecar and IQ sidecar run as-is (ZMQ + numpy, no platform-specific audio). PulseAudio-style audio sidecar replacements are planned (WASAPI/VB-Cable for Windows, BlackHole/CoreAudio for macOS).  
> See [Roadmap](#roadmap).

## What problem does this solve?

ExpertSDR3 exposes the radio over the **TCI** WebSocket protocol on port
50001.  ExpertSDR3 will only stream audio (or IQ) to **one** TCI client
at a time -- specifically the client that asserted PTT.  That makes it
impossible to naively run multiple programs (e.g. a CAT controller plus
a digital-mode modem plus a GR flowgraph) against the radio.

Hamlib's modified `tci2.c` driver fixes the CAT half: rigctld owns the
one allowed TCI connection and exposes a normal Hamlib radio on port
4532.  But the audio and IQ still have to get to and from clients
somehow.

These sidecars are the missing pieces.  They connect to rigctld over
small TCP sidechannels (one for audio, one for IQ; both independent),
forward RX audio frames into a virtual audio sink, ship modem TX audio
back through rigctld, and publish RX IQ over ZMQ for SDR consumers.
Modems talk to the radio exactly like any other soundcard-and-CAT
setup; GR consumes IQ exactly like any other ZMQ source.

```
       ┌──────────────┐    TCI         ┌─────────────┐
       │  ExpertSDR3  │◀──WebSocket───▶│   rigctld   │◀── CAT (JS8Call, etc.) :4532
       │  (port 50001)│                │             │
       └──────────────┘                └──┬───────┬──┘
                                          │       │
                          audio :4534 ────┘       └──── iq :4535
                                          │              │
                                          ▼              ▼
                              ┌──────────────────┐  ┌──────────────────┐
                              │  audio sidecar   │  │ tci-iq-sidecar.py│
                              │ (soundcard or GR)│  │                  │
                              └────┬────────┬────┘  └─────────┬────────┘
                       tci-rx ◀────┘        └───▶ tci-tx      │
                       (sink)                     (sink)      │
                          ▲                          ▲        │ ZMQ PUB :5555
                          │                          │        │ (complex float32)
                          │      modem reads RX,     │        │
                          └─────  writes TX  ────────┘        ▼
                                                       GR flowgraph,
                                                       tci-iq-viewer.py,
                                                       any ZMQ consumer
```

The audio sidecar comes in two flavors:
`tci-audio-soundcard-sidecar.py` (PulseAudio null sinks for JS8Call
etc.) or `tci-audio-gr-sidecar.py` (ZMQ for GNU Radio).  Either-or:
both connect to rigctld's `audio_port` (`:4534`) and only one client is
allowed at a time.

The IQ sidecar is independent: it connects to rigctld's `iq_port`
(`:4535`) and can run alongside either audio sidecar.

## Requirements

Common:

- Python 3.8+
- `numpy`
- A Hamlib build with the TCI 2.0 driver and the patches required by
  these sidecars.  See [Hamlib build](#hamlib-build).
- An Expert Electronics SDR (or Apache Labs ANAN with TCI-capable
  firmware) running with TCI enabled on the default port 50001.

For `tci-audio-soundcard-sidecar.py` (PulseAudio audio sidecar):

- Linux desktop with PulseAudio or PipeWire (with `pipewire-pulse`)
- `pulseaudio-utils` (`pactl`, `pacat`, `parec`)

For `tci-audio-gr-sidecar.py` (GR-style audio sidecar) and the IQ
sidecar:

- `pyzmq`

For the GR-side tools (`tci-iq-viewer.py`, `tci-audio-gr-tester.py`):

- GNU Radio 3.10+ with `gr-zeromq` and `qtgui`
- PyQt5 (or PyQt6, whatever GR was built against)

Install runtime tools:

```bash
# Debian / Ubuntu / Mint
sudo apt install pulseaudio-utils python3-numpy python3-zmq \
                 gnuradio python3-pyqt5

# Fedora / RHEL
sudo dnf install pulseaudio-utils python3-numpy python3-zmq \
                 gnuradio python3-pyqt5

# Arch
sudo pacman -S libpulse python-numpy python-pyzmq \
               gnuradio python-gnuradio gnuradio-companion python-pyqt5
```

If you only want audio (no IQ), skip the `pyzmq` / `gnuradio` /
`pyqt5` packages.  If you only want IQ (no audio), skip the
`pulseaudio-utils` package.

## Quick start

The fast path: use the bundled `tci.sh` helper which manages
rigctld + both sidecars as a unit.

1. **Start ExpertSDR3** with TCI enabled on port 50001.

2. **Start everything**:

   ```bash
   ./tci.sh start     # idempotent; cleans up first
   ```

   This brings up rigctld, the audio sidecar, and the IQ sidecar.
   You can disable either by editing the top of `tci.sh`
   (`ENABLE_AUDIO=0`, `ENABLE_IQ=0`), and pick which audio
   sidecar via `AUDIO_BACKEND=pulseaudio` (default; for JS8Call
   etc.) or `AUDIO_BACKEND=gr` (for GNU Radio flowgraphs).

3. **Configure your ham-radio software**:
   - With `AUDIO_BACKEND=pulseaudio`:
     - Audio input (RX): `tci-rx.monitor`
     - Audio output (TX): `tci-tx`
   - With `AUDIO_BACKEND=gr`:
     - GR `zmq_sub_source` on `tcp://localhost:5557` for RX audio
     - GR `zmq_push_sink` on `tcp://localhost:5558` for TX audio
     - Both float32 mono 8 kHz
   - CAT: Hamlib `rigctld` (network) at `localhost:4532`

4. **Optional: watch the band on the IQ stream**:

   ```bash
   python3 tci-iq-viewer.py
   ```

5. **Optional: GR audio loopback test** (only with
   `AUDIO_BACKEND=gr`):

   ```bash
   python3 tci-audio-gr-tester.py
   ```

That's it.  PTT triggered by the modem (via CAT) keys the radio; modem
TX audio flows out the antenna; received audio shows up in the modem's
waterfall; and the IQ stream is available on `tcp://localhost:5555`
for any ZMQ-aware consumer.

### `tci.sh` commands

```bash
./tci.sh start     # launch rigctld + enabled sidecars (idempotent)
./tci.sh stop      # tear down everything, unload the PulseAudio sinks
./tci.sh restart   # stop + start
./tci.sh status    # what's running, sinks, listening ports, last log lines
./tci.sh log       # tail -F the audio sidecar log
./tci.sh iqlog     # tail -F the IQ sidecar log
```

Edit the variables at the top of `tci.sh` to point at your rigctld
binary and Hamlib library locations.  ExpertSDR3 must already be
running before `tci.sh start`; the script doesn't manage ExpertSDR3.

### Manual start (without `tci.sh`)

If you want to run things by hand:

```bash
# rigctld with both sidechannels enabled
rigctld -m 12 -r localhost:50001 -t 4532 \
        -C audio_port=4534 \
        -C iq_port=4535 -C iq_rate=192000

# audio sidecar -- soundcard flavor (creates tci-rx, tci-tx PA sinks)
python3 tci-audio-soundcard-sidecar.py \
    --rigctld-host localhost --rigctld-port 4534 \
    --name tci --tx-gain-db 20 --rx-gain-db 0
# (or use tci-audio-gr-sidecar.py instead for GNU Radio)

# IQ sidecar (publishes complex float32 on tcp://*:5555)
python3 tci-iq-sidecar.py \
    --rigctld-host localhost --rigctld-port 4535 \
    --zmq-bind 'tcp://*:5555'
```

## Configuration

### Audio sidecar -- soundcard / PulseAudio (`tci-audio-soundcard-sidecar.py`)

```
tci-audio-soundcard-sidecar.py [-h]
                               [--rigctld-host HOST]   default: localhost
                               [--rigctld-port PORT]   default: 4534
                               [--name NAME]           default: tci
                                                       (creates NAME-rx, NAME-tx)
                               [--tx-gain-db DB]       default: +20.0 dB (=10x linear)
                               [--rx-gain-db DB]       default:   0.0 dB (=unity)
```

For modem-style consumers (JS8Call, fldigi, WSJT-X) that want a
soundcard-shaped interface.  Use this OR the GR audio sidecar
below, not both -- rigctld's audio sidechannel accepts a single
client.

#### TX gain (the ExpertSDR3 quiet-audio gotcha)

ExpertSDR3 silently drops TX audio that's below some internal threshold
-- no error, no log entry, just zero output power.  JS8Call and several
other modems output audio at around -20 dBFS, well below that
threshold.  The default **+20 dB** of TX gain (with hard clipping at
int16 saturation) brings JS8Call's audio up to the level ExpertSDR3
actually transmits.

If you're using software that already outputs near full-scale audio you
can back this off.  If a modem outputs even quieter audio than JS8Call
you may need more.

#### RX gain

Default is 0 dB (unity).  Provided for symmetry.  Use a small positive
value to boost RX into a modem that wants louder input, or a negative
value to attenuate.

Both gains have automatic clip-to-int16 saturation so you can't
introduce wraparound distortion.

### Audio sidecar -- GNU Radio (`tci-audio-gr-sidecar.py`)

```
tci-audio-gr-sidecar.py [-h]
                        [--rigctld-host HOST]     default: localhost
                        [--rigctld-port PORT]     default: 4534
                        [--zmq-rx-bind ENDPOINT]  default: tcp://*:5557
                        [--zmq-tx-bind ENDPOINT]  default: tcp://*:5558
                        [--tx-gain-db DB]         default: +20.0 dB
                        [--rx-gain-db DB]         default:   0.0 dB
```

For GNU Radio flowgraphs that want to consume RX audio and produce
TX audio as ZMQ streams instead of as PulseAudio devices.  Same
TCI sidechannel as the PulseAudio sidecar; you choose which one to
run.

- **RX**: `STREAM_RX_AUDIO` frames from rigctld are converted to
  float32 mono and published on the RX ZMQ PUB socket.  GR
  consumes via `zmq_sub_source` (item type `float`, vec length 1,
  sample rate 8000).
- **TX**: GR pushes float32 mono audio to the TX ZMQ PULL socket
  via `zmq_push_sink`.  The sidecar converts to int16, accumulates
  into a buffer, and ships frames in response to the radio's
  `STREAM_TX_CHRONO` requests.  PTT_STATE: ON flushes the TX buffer
  the same way the PulseAudio sidecar does.
- **Sample rate**: 8 kHz mono throughout.  GR flowgraphs that work
  at other rates resample at the boundary; the sidecar does NOT
  resample.

ZMQ behaviour mirrors the IQ sidecar: PUB drops oldest under
backpressure on RX; PULL has a high-water mark on TX.  Lossy is
correct for live audio.

### GR audio tester (`tci-audio-gr-tester.py`)

```
tci-audio-gr-tester.py [-h]
                       [--zmq-rx tcp://localhost:5557]
                       [--zmq-tx tcp://localhost:5558]
                       [--cat-host localhost] [--cat-port 4532]
```

Sample GR application that demonstrates the GR audio sidecar
end-to-end:

- RX audio FFT (0..4 kHz) plus a time-domain waveform of the same
  signal -- shows what's coming through the receiver.
- 1 kHz sine generator gated by a PTT toggle button.  Click PTT,
  it sends `T 1` to rigctld and pushes the tone out of `zmq_push_sink`
  to the sidecar; click again, sends `T 0`.
- TX volume slider for amplitude control.

Run on the same host as the sidecar (loopback ZMQ + rigctld).  For
a remote setup, SSH-tunnel ports 5557, 5558, and 4532.

### IQ sidecar

```
tci-iq-sidecar.py [-h]
                  [--rigctld-host HOST]    default: localhost
                  [--rigctld-port PORT]    default: 4535
                  [--zmq-bind ENDPOINT]    default: tcp://*:5555
```

The IQ sidecar reads `STREAM_IQ` frames from rigctld and republishes
them as `complex float32` (interleaved I and Q, 8 bytes per complex
sample) over a ZMQ PUB endpoint.  No gain stages, no virtual sound
device, no TX path -- just stream-format conversion + ZMQ pub.

Sample rate, sample format, and channel count are negotiated by
rigctld with ExpertSDR3 (`-C iq_rate=` on rigctld; default 192000).
The sidecar reads what the radio actually sends from the frame
headers and converts to `complex float32` regardless of what
ExpertSDR3 sends.

ZMQ PUB is lossy under backpressure: if a subscriber falls behind,
old messages are dropped on the publisher side.  This is the right
behaviour for a live SDR feed -- we'd rather lose old samples than
stall the radio.

### IQ viewer

```
tci-iq-viewer.py [-h]
                 [--zmq tcp://localhost:5555]
                 [--cat-host localhost] [--cat-port 4532]
                 [--rate 192000] [--freq INITIAL_HZ]
                 [--fft-size 1024] [--fft-ymin -130] [--fft-ymax -40]
                 [--fft-autoscale]
                 [--fft-average 0.2]
                 [--wf-min -125] [--wf-max -60]
```

Live FFT + waterfall in a Qt window.  Polls rigctld every 500 ms for
the current dial frequency so the X axis stays in sync with whatever
JS8Call / ExpertSDR3 is tuned to.  Left-click on the FFT or
waterfall to retune (sends `F nnnnn` to rigctld).

Defaults are tuned for ExpertSDR3 IQ levels: noise floor sits around
-125 dB, strong HF signals come in around -60 to -50 dB, so the
visible window is -130 to -40.  Pass `--fft-autoscale` if you'd
rather GR figure out the ranges itself, or override the bounds with
`--fft-ymin` / `--fft-ymax` and `--wf-min` / `--wf-max`.

## Wire protocol (sidecar <-> rigctld)

Pure binary, length-framed TCI frames in **both directions**.  One
parser, one mental model.  No text framing, no delimiters, no demuxing.

This matters because audio and IQ payloads contain arbitrary bytes
(including 0x0A, ':', whitespace).  Earlier prototypes that mixed
text lines like `TX_CHRONO 0 512\n` with binary RX_AUDIO frames on
the same socket got shredded the moment a sample value happened to
contain a newline byte.  Going all-binary with the existing TCI
64-byte header eliminates that entire class of bug.

The audio and IQ sidechannels are **independent** -- separate TCP
ports, separate sidecar processes -- but they speak the same wire
format and share the same `stream_type` namespace.  rigctld
dispatches inbound binary frames to whichever sidechannel matches
the frame's `stream_type` field.

### Frame format

Every message is a **64-byte header followed by 0..N payload bytes**.

Header (16 little-endian uint32 words, total 64 bytes):

| Offset | Word | Field        | Audio / IQ frames                     | Control frames |
|--------|------|--------------|---------------------------------------|----------------|
| 0      | [0]  | receiver     | trx index                             | trx index      |
| 4      | [1]  | sample_rate  | Hz                                    | 0              |
| 8      | [2]  | format       | 0=int16  1=int24  2=int32  3=float32  | 0              |
| 12     | [3]  | codec        | 0                                     | 0              |
| 16     | [4]  | crc          | 0                                     | 0              |
| 20     | [5]  | length       | samples in payload                    | control value  |
| 24     | [6]  | stream_type  | 0=IQ  1=RX_AUDIO  2=TX_AUDIO          | 3=TX_CHRONO  4=PTT_STATE |
| 28     | [7]  | channels     | 1 (audio) or 2 (complex IQ)           | 1              |
| 32..63 |      | reserved     | zero-filled                           | zero-filled    |

Payload size = `length × channels × sample_bytes(format)` for audio
and IQ frames, **0** for control frames.

### Stream types

| stream_type | name       | direction         | sidechannel | length means        | payload |
|-------------|------------|-------------------|-------------|---------------------|---------|
| 0           | IQ         | rigctld → sidecar | iq          | samples in payload  | yes     |
| 1           | RX_AUDIO   | rigctld → sidecar | audio       | samples in payload  | yes     |
| 2           | TX_AUDIO   | sidecar → rigctld | audio       | samples in payload  | yes     |
| 3           | TX_CHRONO  | rigctld → sidecar | audio       | samples requested   | no      |
| 4           | PTT_STATE  | rigctld → sidecar | audio       | 0=PTT off, 1=PTT on | no      |

Values 0..2 match the TCI protocol's own `StreamType` enum and are
forwarded as-is from the radio.  3..4 are hamlib-internal control
frames between rigctld and the audio sidecar.  Values 5..255 are
reserved for future hamlib-internal control frames (likely
candidates: VFO change, mode change, sample-rate negotiation).  The
32 reserved bytes in the header give room for parameters that don't
fit in `length`.

Receivers MUST silently skip unknown stream_type values so newer
senders coexist with older receivers.

### About PTT_STATE (control frame)

ExpertSDR3 may or may not echo TRX state back over the TCI WebSocket;
we can't depend on it.  rigctld is the authoritative PTT source on
this socket regardless: when an application calls `tci2_set_ptt()`,
that's when the radio is told to key, and that's when the sidecar
needs to know.

So `tci2_set_ptt()` emits a `STREAM_PTT_STATE` frame on every PTT
edge (only on edges, not on every set_ptt call).  The sidecar uses
this signal to **flush its TX capture buffer** at the start of each
transmission.

Without that flush, the sidecar would ship whatever silence its
parec capture pipeline accumulated while idle, delaying live audio
at the start of every TX.  parec keeps reading samples from
tci-tx.monitor whenever no one is playing -- those are zeros, but
they are queued bytes -- and on the next PTT-on those queued zeros
would arrive at the radio first.  That's exactly the bug PTT_STATE
prevents.

### About the IQ stream

`STREAM_IQ` (stream_type=0) frames are forwarded by rigctld
verbatim from the radio.  ExpertSDR3 chooses the sample rate (set
via `IQ_SAMPLERATE` — rigctld's `-C iq_rate=` flag, default
192000), the format (typically `float32`), and the channel count
(`2` for complex).  Sample rates supported are 48000 / 96000 /
192000 / 384000 Hz.

**RX only — there is no TX IQ.**  TCI's `StreamType` enum defines
exactly one IQ stream (`IQ_STREAM = 0`), and it is unidirectional
from radio to client.  There is no spec-defined way for a client
to push IQ samples back to the radio for transmission, so the IQ
sidechannel and the IQ sidecar process only deal with RX.

This is a **TCI 2.0 protocol limitation**, not a Hamlib or sidecar
design choice.  Adding TX IQ outside the spec would break interop
with non-Expert-Electronics TCI implementations (e.g. Apache Labs
ANAN with TCI firmware).

TX of arbitrary baseband signals remains available via the audio
sidecar's `TX_AUDIO_STREAM` path, which ExpertSDR3 accepts at
8 kHz mono for HF digital modes.

## Sidecar internals (one screen each)

### `tci-audio-soundcard-sidecar.py` (audio, soundcard / PulseAudio)

- One thread reads the rigctld socket, parses 64-byte headers,
  dispatches by `stream_type`.
- `RX_AUDIO` frames have their payload converted (int16/int24/int32/
  float32 -> int16 mono), gain-adjusted, pushed into the RX buffer.
- `TX_CHRONO` frames push a single integer onto a queue.Queue (the
  number of samples the radio wants).
- `PTT_STATE: ON` triggers an atomic clear of the TX buffer.
- A `pacat` subprocess plays the RX buffer into the `tci-rx` null
  sink; modems read from `tci-rx.monitor`.
- A `parec` subprocess captures from the `tci-tx.monitor` null source
  into the TX buffer.
- A TX worker thread blocks on the TX_CHRONO queue.  When a request
  arrives it pulls exactly that many samples from the TX buffer (or
  zeros if the buffer doesn't have enough), applies gain, and ships
  a `STREAM_TX_AUDIO` frame back to rigctld.
- Sample rate is 8 kHz mono throughout (matches what ExpertSDR3
  negotiates for HF digital modes).

That's the whole thing.  No demuxer state, no resync logic, no special
cases.  ~500 lines of Python including comments.

### `tci-audio-gr-sidecar.py` (audio for GNU Radio)

Same shape as `tci-audio-soundcard-sidecar.py` but with the PulseAudio plumbing
replaced by ZMQ:

- Same `rigctld_reader` thread, same dispatch by `stream_type`,
  same PTT_STATE buffer-flush logic.
- RX: `STREAM_RX_AUDIO` payload converted from int16 (or whatever
  format ExpertSDR3 negotiated) to float32, sent as one ZMQ PUB
  message on `tcp://*:5557` per radio frame.
- TX: a separate thread polls a ZMQ PULL socket on `tcp://*:5558`
  for float32 messages from GR, converts each to int16 + gain,
  appends to the TX buffer.
- TX worker behaves the same as the PulseAudio sidecar's: pulls
  N samples on each `STREAM_TX_CHRONO`, ships `STREAM_TX_AUDIO`.
  When the buffer has fewer than N samples but more than zero,
  it ships what it has padded with silence (real audio devices
  do this; switching to all-silence under partial-fill produces
  worse on-air results).

ZMQ HWMs are 64 messages each side; LINGER=0.  Lossy on both ends.
~400 lines of Python.

### `tci-iq-sidecar.py` (IQ)

Even simpler -- one-direction:

- One thread reads the rigctld socket, parses 64-byte headers,
  filters for `stream_type == STREAM_IQ`.
- IQ payload is decoded according to the frame's `format` field
  (int16/int24/int32/float32) and `channels` field (1=real, 2=complex)
  and converted to numpy `complex64` (interleaved I,Q,I,Q,... at 8
  bytes per complex sample).
- The complex64 buffer is published as a single ZMQ PUB message.
- ZMQ PUB is configured with `SNDHWM=64` and `LINGER=0` -- it drops
  old messages under backpressure and doesn't wait on shutdown.

No virtual device, no TX path, no buffer flushing, no gain stage.
~300 lines of Python.

## Hamlib build

The Hamlib `tci2.c` you need is the one in this work tree.  The
required changes vs. unmodified upstream:

- Single-reader queue between the WS-reader thread and the CAT
  thread (avoids a race where the audio thread eats CAT replies).
- TCP-stream reassembly of TCI frames coming from the audio sidecar
  (a partial `recv()` ships a malformed WebSocket frame and
  ExpertSDR3 silently drops the audio -- TX appears to engage but
  emits 0 W).
- Idempotent audio listen-socket setup so reconnecting CAT clients
  don't trip "address already in use".
- `tci2_send` is internally thread-safe so the reader thread and
  the CAT thread can both write to the WebSocket.
- TX-from-sidecar pumping runs every loop iteration, not only on
  WebSocket-poll timeout.
- TX_CHRONO is forwarded to the audio sidecar as a binary
  `STREAM_TX_CHRONO` control frame (not a text line) -- this is
  what makes the binary-only sidechannel possible.
- PTT edges are forwarded to the audio sidecar as
  `STREAM_PTT_STATE` control frames.
- `iq_port` and `iq_rate` config params, an IQ-only TCP listen
  socket, and dispatch of `stream_type=0` (IQ) frames to that fd.
- Internal WS receive buffer raised to 32 KB to accommodate IQ
  frames at 384 kHz (default 8 KB was sized for audio only).

If you're building Hamlib from source against this work tree:

```bash
cd Hamlib
make -C rigs/dummy tci2.lo
rm -f rigs/dummy/.libs/libhamlib-dummy.a rigs/dummy/libhamlib-dummy.la \
      src/libhamlib.la src/.libs/libhamlib.so.5*
( cd rigs/dummy && make libhamlib-dummy.la )
( cd src && make libhamlib.la )
touch tests/rigctld.c && make -C tests rigctld
```

The aggressive `rm` is necessary because Hamlib's autotools/libtool
machinery can silently link a stale convenience archive into the
shared library, even after `tci2.c` has been recompiled.  After
building, verify:

```bash
strings src/.libs/libhamlib.so.5.0.0 | grep -E "PTT_STATE forward|IQ sidecar listening"
```

Both strings should appear.

## JS8Call audio buffer setting (recommended for all users)

By default JS8Call lets Qt pick the PulseAudio buffer size, which on
Linux/PA defaults to ~2 seconds.  You can see this on the JS8Call audio
stream as `pulse.attr.tlength = 192000` (= 2.0 s @ 48 kHz mono).

Add this to `~/.config/JS8Call.ini`:

```
[Tune]
Audio\OutputBufferMs=200
```

Restart JS8Call after editing.

The section is `[Tune]` (not `[Audio]`) and the key is
`Audio\OutputBufferMs` with a backslash, because of how QSettings
nested groups map to .ini.  Inside `mainwindow.cpp`, JS8Call does
`m_settings->beginGroup("Tune")` and then
`m_settings->value("Audio/OutputBufferMs")`, so the full Qt key path
is `Tune/Audio/OutputBufferMs`, which in INI form becomes the section
+ backslash key shown above.  Putting it under `[Audio]` looks right
and does nothing.

This change is recommended even though, in our testing, **it did not
shrink the observed PTT-on-to-first-audio latency**.  The PA buffer
shrinks (we measured `tlength = 19200` after the change), but JS8Call
still appears to introduce ~2 seconds of internal pre-roll before the
first sample is written.  The benefit of the setting is that it makes
the PA-side latency deterministic at 200 ms instead of relying on
whatever Qt chose for your distro and PA version.

If you see TX-audio underruns or glitches, push the value up to 300--
500 ms; if you see late on-air timing relative to other stations, push
it down toward 100 ms.

## Open question: the ~2 s TX pre-roll

Across a series of bench tests we observed ~2 seconds between
`PTT_STATE: ON` (rigctld emits this when the application calls set_ptt)
and the first non-silence audio frame reaching the radio.  This is true
both before and after the JS8Call buffer fix above.  Real-world test
on 40 m: clean CQ via this pipeline got replies, **confirming on-air
decode at remote receivers** -- so the delay is benign in practice and
appears to be intrinsic JS8Call timing that soundcard users have too.

We chose not to chase this further once on-air decode was confirmed.
The investigation chain is preserved in `PR_NOTES.md`.  Possible
sources of the pre-roll worth checking later:

- JS8Call's slot-alignment scheduler
- QAudioOutput stream-startup latency (start-when-format-set vs.
  start-on-first-write)
- PipeWire 48->8 kHz resample path warmup

## Verifying the install

End-to-end checks that rule out broken classes of failure:

- **CAT works**:
  ```bash
  echo 'f' | nc -w 1 localhost 4532    # should print the dial freq
  echo 'm' | nc -w 1 localhost 4532    # mode + filter width
  ```

- **RX audio is flowing into the audio sidecar**:
  ```bash
  timeout 3 parec --device=tci-rx.monitor --rate=8000 --channels=1 \
      --format=s16le > /tmp/rx_check.raw
  python3 -c "
  import struct
  d = open('/tmp/rx_check.raw','rb').read()
  s = struct.unpack('<' + str(len(d)//2) + 'h', d)
  peak = max(abs(x) for x in s) if s else 0
  nz = sum(1 for x in s if x != 0)
  print(f'samples={len(s)}, peak={peak}, nonzero={nz}')
  "
  ```
  Expect 24000 samples (3 s × 8 kHz), peak well above 100, fraction
  nonzero > 95 %.

- **TX audio reaches the radio**: connect a spectrum analyzer (or a
  receiver) to the rig.  Have something play a tone into `tci-tx`,
  raise PTT via CAT, watch the SSA.  See `ssa_tx_test.py` in this
  repo (specific to a Siglent SSA3032X Plus on 10.1.1.60; edit for
  your instrument).

- **IQ is flowing into the IQ sidecar and out via ZMQ**:
  ```bash
  python3 -c "
  import zmq, time, numpy as np
  s = zmq.Context.instance().socket(zmq.SUB)
  s.setsockopt(zmq.SUBSCRIBE, b'')
  s.setsockopt(zmq.RCVTIMEO, 2000)
  s.connect('tcp://localhost:5555')
  end = time.time() + 3
  msgs = samples = 0
  while time.time() < end:
      try: m = s.recv()
      except zmq.Again: continue
      msgs += 1
      samples += len(m) // 8   # complex64 = 8 bytes
  print(f'{msgs} ZMQ messages, {samples} complex samples in 3 s')
  print(f'effective rate: {samples/3:.0f} Hz')
  "
  ```
  Expect ~280 messages, ~580k samples, effective rate close to the
  configured `iq_rate` (default 192000).

- **GR sees the IQ stream**:
  ```bash
  python3 tci-iq-viewer.py
  ```
  Should show a live FFT and waterfall.  Tune the radio (in
  JS8Call, ExpertSDR3's GUI, or by clicking on the FFT) and watch
  the X axis follow.

- **The ultimate audio test**: in JS8Call, call CQ on a band where
  someone is listening.  If you get replies, the entire audio
  pipeline is working on-air.

The sidecars' stdout is the easiest live diagnostic.  `tci.sh log`
follows the audio sidecar; `tci.sh iqlog` follows the IQ sidecar.
Look for:

- Audio sidecar: `RX #N` lines every ~64 ms while the radio is
  receiving (proves the RX audio path); `PTT_STATE: ON` and `PTT-on
  edge: flushing N bytes` exactly once per PTT cycle (proves
  rigctld is emitting the control frame); `TX #N: ... (audio,
  peak=..., silence_total=...)` while keying with `silence_total`
  not growing (proves no dropouts mid-transmission).
- IQ sidecar: `IQ stream: sample_rate=...` line when the radio
  starts streaming, then periodic `stats:` lines reporting frames
  and bytes per 5-second window.

## Roadmap

### Planned

- **Windows PulseAudio sidecar replacement** — same wire protocol,
  different audio plumbing (likely WASAPI loopback or VB-Audio
  Cable).  The GR audio sidecar already runs on Windows (Python +
  pyzmq, no platform-specific audio), so Windows GR users have a
  working audio path today.  This Windows entry is specifically for
  the soundcard-style consumers (JS8Call, fldigi, WSJT-X running
  natively on Windows).
- **macOS PulseAudio sidecar replacement** — likely BlackHole or a
  CoreAudio aggregate device.  Same caveat as Windows: the GR audio
  sidecar already works on macOS; this is for soundcard-style
  consumers.

The IQ sidecar and the GR audio sidecar are **portable as-is** (ZMQ +
numpy, no platform-specific audio APIs).  Whatever PulseAudio
sidecar replacement comes for Windows or macOS, the IQ and GR audio
sidecars will run unchanged alongside it.

The PulseAudio sidecar's structure is deliberately split between the
protocol core (parser / dispatcher / buffers) and the platform-
specific audio glue (`pacat` / `parec` / `pactl` subprocess
invocations).  The Windows and macOS audio sidecars will reuse the
protocol core verbatim.

### Not on the Roadmap

**TX IQ** is not on the roadmap.  The TCI 2.0 spec defines `IQ_STREAM`
as **RX-only** (radio → client); there is no `TX_IQ_STREAM`.  Adding
one outside the spec would break interop with non-Expert-Electronics
TCI implementations (e.g. Apache Labs ANAN with TCI firmware) and
isn't actionable without cooperation from the TCI spec authors.

TX of arbitrary baseband signals remains available via `TX_AUDIO_STREAM`
in the audio sidecar, which ExpertSDR3 accepts at 8 kHz mono for HF
digital modes.

## License

MIT.  See `LICENSE`.

## Acknowledgements

- Expert Electronics for the [TCI 2.0 protocol
  specification](https://eesdr.com/en/).
- [eesdr-tci](https://github.com/maksimus1210/TCI) and the Python
  `eesdr-tci` library for showing how a working TCI client behaves.
- The Hamlib project for `tci2.c` -- the patched copy this sidecar
  depends on is a fork of that work.
