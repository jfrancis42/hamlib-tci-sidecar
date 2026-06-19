# Hamlib TCI Audio Sidechannel Protocol

This document specifies the wire protocol used between rigctld (Hamlib's
TCI 2.0 backend) and the audio sidecar process.  It is the canonical
reference; both sides must agree on framing and stream-type semantics.

## Goals

- **Single framing.** One length-framed binary format covers audio
  payloads and control messages alike.  No text/binary demuxing.
- **Symmetric.** Same frame format in both directions.
- **Extensible.** New control or stream types are new `stream_type`
  values; existing code keeps working.
- **Audio-clean.** Audio data may contain any byte (including 0x0A,
  ':', whitespace).  No delimiter framing on this socket.

## Topology

```
ExpertSDR3 ──TCI WebSocket──> rigctld ──binary TCI frames──> sidecar
  :50001       (single conn)   :4534         (TCP, single conn)    │
                                                                   ▼
                                                        PulseAudio sinks
                                                        tci-rx, tci-tx
                                                                   │
                                                                   ▼
                                                       JS8Call / fldigi /
                                                       WSJT-X / etc.
```

rigctld owns the only TCI WebSocket to ExpertSDR3.  The sidecar talks
**only** to rigctld; it never connects to ExpertSDR3 directly.

The audio sidechannel is a single TCP connection.  rigctld listens on
`-C audio_port=N` (default 4534) and accepts one client; the sidecar
connects to it.

## Frame format

Every message is a **64-byte header followed by 0..N bytes of payload**.

Header layout (16 little-endian uint32 words, total 64 bytes):

| Offset | Word | Field        | Audio frames               | Control frames |
|--------|------|--------------|----------------------------|----------------|
| 0      | [0]  | receiver     | trx index (0)              | trx index (0)  |
| 4      | [1]  | sample_rate  | Hz                         | 0              |
| 8      | [2]  | format       | 0=int16  1=int24  2=int32  3=float32 | 0    |
| 12     | [3]  | codec        | 0                          | 0              |
| 16     | [4]  | crc          | 0                          | 0              |
| 20     | [5]  | length       | samples in payload         | control value (see below) |
| 24     | [6]  | stream_type  | 1=RX_AUDIO  2=TX_AUDIO     | 3=TX_CHRONO  4=PTT_STATE |
| 28     | [7]  | channels     | 1 for mono                 | 1              |
| 32..63 |      | reserved     | zero-filled                | zero-filled    |

Payload size = `length × channels × sample_bytes(format)` for audio
frames, **0** for control frames.  `sample_bytes` is 2/3/4/4 for
formats 0/1/2/3.

**Receivers MUST treat the reserved 32 bytes as opaque** so future
fields can be added without breaking existing implementations.

## Stream types

| stream_type | name       | direction         | length means          | payload |
|-------------|------------|-------------------|-----------------------|---------|
| 1           | RX_AUDIO   | rigctld → sidecar | samples in payload    | yes     |
| 2           | TX_AUDIO   | sidecar → rigctld | samples in payload    | yes     |
| 3           | TX_CHRONO  | rigctld → sidecar | samples requested     | no      |
| 4           | PTT_STATE  | rigctld → sidecar | 0=PTT off, 1=PTT on   | no      |

### RX_AUDIO (1)

Audio captured from the radio.  Forwarded by rigctld unchanged from
ExpertSDR3's RX_AUDIO_STREAM TCI binary frames.  The sidecar pushes
this data to its `<name>-rx` PulseAudio null sink so client apps can
read it from `<name>-rx.monitor`.

### TX_AUDIO (2)

Audio to be transmitted.  The sidecar reads from its `<name>-tx`
PulseAudio null sink (via `<name>-tx.monitor`) and ships TX_AUDIO
frames in response to TX_CHRONO requests.  rigctld wraps each frame
in a TCI WebSocket binary frame to ExpertSDR3.  Sample rate must
match the rate ExpertSDR3 negotiated for transmit (typically 8 kHz
for HF digital).

### TX_CHRONO (3)

Pacing pulse from ExpertSDR3.  ExpertSDR3 emits TX_CHRONO whenever
it needs the next chunk of TX audio.  rigctld translates the TCI
text command `TX_CHRONO:trx,samples;` into a TCI_STREAM_TX_CHRONO
binary frame with `length = samples` and forwards it to the sidecar.

The sidecar should respond with a TX_AUDIO frame containing exactly
`length` samples.  If insufficient audio is buffered, the sidecar
sends silence (zero-filled samples) for that frame.

### PTT_STATE (4)

Authoritative PTT state edge from rigctld to the sidecar.  Emitted by
rigctld in `tci2_set_ptt()` whenever the PTT state changes (only on
edges, not on every set_ptt call).

`length` carries the new state: 0 (PTT off) or 1 (PTT on).

The sidecar uses this to flush its TX capture buffer at the start of
each transmission.  Without this, audio that the sidecar's `parec`
captured while the radio was idle (typically zeros, but sometimes
test signals or feedback) would be transmitted at the start of the
next TX cycle.

This event is rigctld's responsibility because rigctld is the
authoritative PTT source: ExpertSDR3 may or may not echo TRX state
back over TCI, but `tci2_set_ptt` always knows.

## Reserved stream types

Values 5..255 are reserved for future use within this protocol.
Likely candidates: VFO change, mode change, sample-rate negotiation,
IQ stream start/stop.

The 32 bytes of reserved header space are reserved for parameters
that don't fit in `length` (e.g. timestamps, frequency in Hz).

## Error handling

- A frame whose `format`, `channels`, or `length` falls outside
  reasonable bounds is treated as a desync.  Receivers should drop
  one byte and continue scanning.
- A frame whose `stream_type` is unknown should be **silently
  skipped** (consume `length`-derived payload, advance, continue).
  This lets newer senders coexist with older receivers.
- Receivers MUST NOT close the connection on unknown stream types.

## Wire-protocol example

Here is the complete byte layout for a TX_CHRONO requesting 512 samples
on receiver 0:

```
00000000  00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
00000010  00 00 00 00 00 02 00 00  03 00 00 00 01 00 00 00
00000020  00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
00000030  00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
```

Word [5]=0x00000200=512 (length), word [6]=0x00000003=3 (TX_CHRONO),
word [7]=0x00000001=1 (channels).  No payload.

For a PTT-on event:

```
00000000  00 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00
00000010  00 00 00 00 01 00 00 00  04 00 00 00 01 00 00 00
00000020..0000003F  zeros
```

Word [5]=1 (PTT on), word [6]=4 (PTT_STATE).
