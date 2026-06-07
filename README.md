# hamlib-tci-sidecar

An audio bridge that lets standard ham radio software (JS8Call, fldigi,
WSJT-X, etc.) work with Expert Electronics SDRs (SunSDR2 Pro/DX, MB1, etc.)
over Hamlib's TCI 2.0 driver.

> **Status: Linux only at the moment.** Windows and macOS versions are
> planned -- the protocol code is portable but the audio glue is currently
> PulseAudio/PipeWire-specific. See [Roadmap](#roadmap).

## What problem does this solve?

ExpertSDR3 exposes the radio over the **TCI** WebSocket protocol on port
50001. ExpertSDR3 will only stream audio to **one** TCI client at a time --
specifically the client that asserted PTT. That makes it impossible to
naively run multiple programs (e.g. a CAT controller plus a digital-mode
modem) against the radio.

Hamlib's modified `tci2.c` driver fixes the CAT half: rigctld owns the one
allowed TCI connection and exposes a normal Hamlib radio on port 4532. But
the audio still has to get to and from the digital-mode software somehow.

This sidecar is the missing piece. It connects to rigctld over a small TCP
sidechannel, forwards RX audio frames into a virtual audio sink the modem
software can read, and forwards modem TX audio back through rigctld to the
radio. The modem talks to the radio exactly like it would with any other
soundcard-and-CAT setup; the sidecar makes that illusion possible.

```
        ┌──────────────┐     TCI          ┌────────────┐
        │  ExpertSDR3  │◀────WebSocket───▶│  rigctld   │
        │  (port 50001)│                  │ (port 4532)│◀── CAT (JS8Call, etc.)
        └──────────────┘                  └─────┬──────┘
                                                │ TCP audio
                                                │ sidechannel
                                                │ (port 4534)
                                                ▼
                                       ┌─────────────────┐
                                       │   this sidecar  │
                                       └────┬───────┬────┘
                            tci-rx (sink)   │       │   tci-tx (sink)
                                            ▼       ▼
                                  ham-radio app reads RX,
                                  writes TX
```

## Requirements

- Linux desktop with PulseAudio or PipeWire (with `pipewire-pulse`)
- Python 3.8+
- `numpy`
- `pulseaudio-utils` (`pactl`, `pacat`, `parec`)
- A Hamlib build with the TCI 2.0 driver and the patches required by this
  sidecar (frame reassembly + sole-reader queue). See
  [Hamlib build](#hamlib-build).
- An Expert Electronics SDR running ExpertSDR3 with TCI enabled on the
  default port 50001.

Install runtime tools:

```bash
# Debian / Ubuntu / Mint
sudo apt install pulseaudio-utils python3-numpy

# Fedora / RHEL
sudo dnf install pulseaudio-utils python3-numpy

# Arch
sudo pacman -S libpulse python-numpy
```

## Install

From PyPI (once published):

```bash
pip install hamlib-tci-sidecar
```

From source:

```bash
git clone https://github.com/jfrancis42/hamlib-tci-sidecar.git
cd hamlib-tci-sidecar
pip install .
```

Either way installs `tci-sidecar-linux` as a console script.

You can also just run the script directly without installing:

```bash
python3 tci-sidecar-linux.py [args]
```

## Quick start

1. **Start ExpertSDR3** with TCI enabled on port 50001.

2. **Start rigctld** against the radio's TCI port, exposing the audio
   sidechannel on port 4534:

   ```bash
   rigctld -m 12 -r localhost:50001 -t 4532 -C audio_port=4534
   ```

   Hamlib model `12` is the TCI 2.0 backend. `-C audio_port=4534` enables
   the audio sidechannel that this sidecar connects to.

3. **Start the sidecar**:

   ```bash
   tci-sidecar-linux
   ```

   It will create two PulseAudio null sinks named `tci-rx` and `tci-tx`,
   connect to rigctld at `localhost:4534`, and start passing audio.

4. **Configure your ham-radio software**:
   - Audio **input** (RX): `tci-rx.monitor`
   - Audio **output** (TX): `tci-tx`
   - CAT: Hamlib `rigctld` (network) at `localhost:4532`, or whatever
     CAT method your software prefers as long as it points at port 4532.

That's it. PTT triggered by the modem (via CAT) keys the radio; modem TX
audio flows out the antenna; received audio shows up in the modem's
waterfall.

## Configuration

```
tci-sidecar-linux [-h]
                  [--rigctld-host HOST]   default: localhost
                  [--rigctld-port PORT]   default: 4534
                  [--name NAME]           default: tci  (creates NAME-rx and NAME-tx)
                  [--tx-gain-db DB]       default: +20.0 dB (=10x linear)
                  [--rx-gain-db DB]       default:   0.0 dB (=unity)
```

### TX gain

ExpertSDR3 silently drops TX audio that's below some internal threshold --
no error, no log entry, just zero output power. JS8Call and several other
modems output audio at around -20 dBFS, well below that threshold. The
default **+20 dB** of TX gain (with hard clipping at int16 saturation)
brings JS8Call's audio up to the level ExpertSDR3 actually transmits.

If you're using software that already outputs near full-scale audio you can
back this off. If a modem outputs even quieter audio than JS8Call you may
need more.

### RX gain

Default is 0 dB (unity). Provided for symmetry. Use a small positive value
to boost RX into a modem that wants louder input, or a negative value to
attenuate.

Both gains have automatic clip-to-int16 saturation so you can't introduce
wraparound distortion.

## Hamlib build

The stock Hamlib `tci2.c` driver has bugs that prevent the audio
sidechannel from working reliably. You need a patched build until the fixes
are merged upstream. The required changes are:

- Single-reader queue between the audio thread and the CAT thread (avoids
  a race where the audio thread eats CAT replies)
- TCP-stream reassembly of TCI frames coming from the sidecar (otherwise a
  partial `recv()` ships a malformed WebSocket frame and ExpertSDR3
  silently drops the audio -- TX appears to engage but emits 0 W)
- Idempotent audio listen-socket setup so reconnecting CAT clients don't
  trip "address already in use"
- `tci2_send` is internally thread-safe so the reader thread and the CAT
  thread can both write to the WebSocket
- TX-from-sidecar pumping runs every loop iteration, not only on
  WebSocket-poll timeout

If you're building Hamlib from source against a known-good tree, run:

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
machinery can silently link a stale convenience archive into the shared
library, even after `tci2.c` has been recompiled. After building, verify:

```bash
strings src/.libs/libhamlib.so.5.0.0 | grep "audio listen socket already up"
```

If that string is present, your shared library has the patches.

## Verifying the install

Three test scripts ship with the project:

- **`test_audio_simple.py`** — Connect to rigctld's audio port, dump 20
  RX_AUDIO frames, report sample stats. No PulseAudio dependency. Useful
  for confirming rigctld is healthy and audio is flowing.

- **`test_tx.py`** — Generate a 1 kHz sine, write it to the `tci-tx`
  PulseAudio sink, key PTT via CAT for a few seconds. Should produce a
  clean carrier 1 kHz above the dial frequency in USB.

- **`monitor_tx.py`** — Watch a Siglent SSA3032X spectrum analyzer at the
  expected TX frequency and report whether RF actually appeared. Use this
  alongside `test_tx.py` to do an end-to-end RF check. (Requires an SSA on
  10.1.1.60 by default; edit the script if you have a different
  instrument.)

## Roadmap

- **Windows version** — same protocol, different audio plumbing (likely
  WASAPI loopback or VB-Audio Cable). Forthcoming.
- **macOS version** — likely BlackHole or a CoreAudio aggregate device.
  Forthcoming.

The single-script structure is deliberate: the protocol logic (TCI frame
codec, TX/RX gain, rigctld TCP framing) is platform-agnostic; only the
audio-sink creation and capture/playback are platform-specific. The
Windows and macOS versions will share the same protocol code.

## License

MIT. See `LICENSE`.

## Acknowledgements

- Expert Electronics for the [TCI 2.0 protocol
  specification](https://eesdr.com/en/).
- [eesdr-tci](https://github.com/maksimus1210/TCI) and the Python
  `eesdr-tci` library for showing how a working TCI client behaves.
- The Hamlib project for `tci2.c` -- the patched copy this sidecar
  depends on is a fork of that work.
