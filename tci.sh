#!/bin/bash
# tci.sh - manage the Hamlib TCI rigctld + sidecar lifecycle.
#
# Manages rigctld plus up to two sidecars:
#   - audio sidecar (tci-audio-soundcard-sidecar.py or
#                    tci-audio-gr-sidecar.py, see AUDIO_BACKEND below)
#                                       -> RX/TX audio for JS8Call etc.
#                                          or for GNU Radio flowgraphs
#   - IQ    sidecar (tci-iq-sidecar.py) -> RX IQ stream for GNU Radio etc.
#
# Both are independent: set ENABLE_AUDIO and ENABLE_IQ near the top of
# this script.  Defaults are both on.
#
# Usage:
#   tci.sh start    -- launch rigctld + enabled sidecars (idempotent)
#   tci.sh stop     -- kill everything, unload PulseAudio modules
#   tci.sh restart  -- stop + start
#   tci.sh status   -- show running state
#   tci.sh log      -- tail -F the audio sidecar log
#   tci.sh iqlog    -- tail -F the IQ sidecar log
#
# Designed to run on the radio host (10.1.1.52).  Expects the rigctld
# binary, libhamlib.so, and the sidecar Python files in /tmp/.  Deploy
# from the dev machine with:
#   scp ~/Dropbox/build/Hamlib/src/.libs/libhamlib.so.5.0.0 \
#       ~/Dropbox/build/Hamlib/tests/.libs/rigctld \
#       ~/Dropbox/build/hamlib-tci-sidecar/tci-audio-soundcard-sidecar.py \
#       ~/Dropbox/build/hamlib-tci-sidecar/tci-audio-gr-sidecar.py \
#       ~/Dropbox/build/hamlib-tci-sidecar/tci-iq-sidecar.py \
#       ~/Dropbox/build/hamlib-tci-sidecar/tci.sh \
#       10.1.1.52:/tmp/
#   ssh 10.1.1.52 'cd /tmp && ln -sf libhamlib.so.5.0.0 libhamlib.so.5'

set -e

# ---------- which sidecars to run ----------

ENABLE_AUDIO=1
ENABLE_IQ=1

# Audio sidecar flavour:
#   pulseaudio  - tci-audio-soundcard-sidecar.py.  Exposes RX/TX audio
#                 as PulseAudio null sinks (tci-rx, tci-tx).  This is
#                 what JS8Call, fldigi, WSJT-X etc. plug into.  Default.
#   gr          - tci-audio-gr-sidecar.py.  Exposes RX audio as a ZMQ
#                 PUB endpoint and accepts TX audio on a ZMQ PULL
#                 endpoint, both as float32 mono 8 kHz.  For GNU Radio
#                 flowgraphs.
# Only one can run at a time -- they both connect to rigctld's audio
# sidechannel, which accepts a single client.
AUDIO_BACKEND=pulseaudio

# ---------- file locations ----------

RIGCTLD=/tmp/rigctld
LIBHAMLIB_DIR=/tmp                 # contains libhamlib.so.5* symlinks
AUDIO_SIDECAR=/tmp/tci-audio-soundcard-sidecar.py
AUDIO_GR_SIDECAR=/tmp/tci-audio-gr-sidecar.py
IQ_SIDECAR=/tmp/tci-iq-sidecar.py

# ---------- ports ----------

TCI_HOST=127.0.0.1
TCI_PORT=50001                     # ExpertSDR3 TCI WebSocket
RIGCTLD_PORT=4532                  # CAT for JS8Call/etc
AUDIO_PORT=4534                    # rigctld <-> audio sidecar
IQ_PORT=4535                       # rigctld <-> IQ    sidecar
TCI_MODEL=12                       # Hamlib model id for TCI 2.0

# ---------- audio sidecar config ----------

TX_GAIN_DB=20
RX_GAIN_DB=0
SINK_NAME=tci                      # creates tci-rx, tci-tx

# ---------- IQ sidecar config ----------

IQ_RATE=192000                     # 48000 / 96000 / 192000 / 384000
ZMQ_BIND="tcp://*:5555"            # GNU Radio: zmq_sub_source @ tcp://HOST:5555

# ---------- audio GR sidecar config (only used when AUDIO_BACKEND=gr) ----------

ZMQ_AUDIO_RX_BIND="tcp://*:5557"   # GR: zmq_sub_source  (RX audio out of sidecar)
ZMQ_AUDIO_TX_BIND="tcp://*:5558"   # GR: zmq_push_sink   (TX audio into sidecar)

# ---------- log files ----------

RIGCTLD_LOG=/tmp/rigctld.err
RIGCTLD_OUT=/tmp/rigctld.out
AUDIO_LOG=/tmp/sidecar.err          # used regardless of AUDIO_BACKEND
AUDIO_OUT=/tmp/sidecar.out
IQ_LOG=/tmp/iq-sidecar.err
IQ_OUT=/tmp/iq-sidecar.out


# ---------- helpers ----------

bold()    { printf '\033[1m%s\033[0m\n' "$*"; }
ok()      { printf '  \033[32mOK\033[0m  %s\n' "$*"; }
warn()    { printf '  \033[33m!!\033[0m  %s\n' "$*"; }
fail()    { printf '  \033[31m!!\033[0m  %s\n' "$*" >&2; }

die() { fail "$*"; exit 1; }

audio_sidecar_path() {
    # The path to the audio sidecar that AUDIO_BACKEND selects.
    case "$AUDIO_BACKEND" in
        pulseaudio) echo "$AUDIO_SIDECAR"    ;;
        gr)         echo "$AUDIO_GR_SIDECAR" ;;
        *)          die "unknown AUDIO_BACKEND='$AUDIO_BACKEND' "
                       "(use 'pulseaudio' or 'gr')" ;;
    esac
}

audio_sidecar_pattern() {
    # pgrep -f pattern that matches THIS backend only.
    case "$AUDIO_BACKEND" in
        pulseaudio) echo "tci-audio-soundcard-sidecar.py" ;;
        gr)         echo "tci-audio-gr-sidecar.py"        ;;
    esac
}

require_files() {
    local missing=0
    for f in "$RIGCTLD" "$LIBHAMLIB_DIR/libhamlib.so.5"; do
        [[ -e $f ]] || { fail "missing: $f"; missing=1; }
    done
    if [[ $ENABLE_AUDIO -eq 1 ]]; then
        local p; p=$(audio_sidecar_path)
        if [[ ! -e $p ]]; then
            fail "missing: $p (AUDIO_BACKEND=$AUDIO_BACKEND, "
                 "or set ENABLE_AUDIO=0)"
            missing=1
        fi
    fi
    if [[ $ENABLE_IQ -eq 1 && ! -e $IQ_SIDECAR ]]; then
        fail "missing: $IQ_SIDECAR (or set ENABLE_IQ=0)"
        missing=1
    fi
    [[ $missing -eq 0 ]] || die "deploy artifacts first (see top of $0)"
}

is_running_rigctld()  { pgrep -x rigctld > /dev/null 2>&1; }
is_running_audio_pa() { pgrep -f "tci-audio-soundcard-sidecar.py" > /dev/null 2>&1; }
is_running_audio_gr() { pgrep -f "tci-audio-gr-sidecar.py" > /dev/null 2>&1; }
is_running_audio()    {
    if [[ $AUDIO_BACKEND == pulseaudio ]]; then
        is_running_audio_pa
    else
        is_running_audio_gr
    fi
}
is_running_iq()       { pgrep -f "tci-iq-sidecar.py" > /dev/null 2>&1; }

unload_pa_sinks() {
    # Remove any tci-rx/tci-tx null sinks (whether ours or stale).
    local n=0
    while IFS= read -r line; do
        local id name
        id=$(awk '{print $1}' <<<"$line")
        name=$(awk '{print $2}' <<<"$line")
        case "$name" in
            "${SINK_NAME}-rx"|"${SINK_NAME}-tx")
                pactl unload-module "$id" 2>/dev/null || true
                n=$((n+1))
                ;;
        esac
    done < <(pactl list sinks short 2>/dev/null || true)
    [[ $n -gt 0 ]] && ok "unloaded $n stale ${SINK_NAME}-rx/${SINK_NAME}-tx sink(s)"
    return 0
}

graceful_kill() {
    # graceful_kill <pgrep-pattern> <human-name> <is-running-fn>
    local pat="$1" name="$2" check="$3"
    if ! $check; then
        ok "$name already stopped"
        return 0
    fi
    local pid
    pid=$(pgrep -f "$pat")
    kill $pid 2>/dev/null || true
    for _ in $(seq 1 10); do
        $check || break
        sleep 0.2
    done
    if $check; then
        warn "$name didn't exit gracefully; killing"
        kill -9 $(pgrep -f "$pat") 2>/dev/null || true
    else
        ok "$name stopped"
    fi
}


# ---------- subcommands ----------

cmd_start() {
    bold "Starting TCI backend"

    require_files

    if is_running_rigctld || is_running_audio_pa || is_running_audio_gr || is_running_iq; then
        warn "found existing instance(s); stopping cleanly first"
        cmd_stop
    fi

    # Sweep stale tci-rx / tci-tx null sinks left over from prior runs.
    # We deliberately do NOT 'systemctl restart pipewire ...': that nukes
    # every other audio client on the box (ExpertSDR3 speaker output,
    # browsers, anything playing audio).  Targeted unload is enough.
    # Only relevant for AUDIO_BACKEND=pulseaudio; the GR backend doesn't
    # touch PulseAudio at all.
    if [[ $ENABLE_AUDIO -eq 1 && $AUDIO_BACKEND == pulseaudio ]]; then
        bold "Cleaning stale audio sinks"
        unload_pa_sinks
        ok "audio sinks ready"
    fi

    # rigctld -- assemble -C flags based on which sidecars are enabled
    bold "Starting rigctld"
    local cflags=()
    if [[ $ENABLE_AUDIO -eq 1 ]]; then
        cflags+=( -C "audio_port=${AUDIO_PORT}" )
    fi
    if [[ $ENABLE_IQ -eq 1 ]]; then
        cflags+=( -C "iq_port=${IQ_PORT}" -C "iq_rate=${IQ_RATE}" )
    fi

    export LD_LIBRARY_PATH="$LIBHAMLIB_DIR"
    nohup "$RIGCTLD" \
        -m "$TCI_MODEL" \
        -r "${TCI_HOST}:${TCI_PORT}" \
        -t "$RIGCTLD_PORT" \
        "${cflags[@]}" \
        -vvvv \
        </dev/null >"$RIGCTLD_OUT" 2>"$RIGCTLD_LOG" &
    disown
    sleep 3

    if ! is_running_rigctld; then
        fail "rigctld failed to start; see $RIGCTLD_LOG"
        tail -20 "$RIGCTLD_LOG" >&2 || true
        return 1
    fi

    local ports="cat :$RIGCTLD_PORT"
    [[ $ENABLE_AUDIO -eq 1 ]] && ports="$ports, audio :$AUDIO_PORT"
    [[ $ENABLE_IQ    -eq 1 ]] && ports="$ports, iq :$IQ_PORT"
    ok "rigctld pid=$(pgrep -x rigctld) ($ports)"

    # CAT sanity: read frequency.  Confirms TCI is connected to ExpertSDR3.
    local freq
    freq=$(echo "f" | nc -w 2 localhost "$RIGCTLD_PORT" 2>/dev/null || true)
    if [[ -z $freq ]]; then
        warn "rigctld is up but CAT 'f' returned nothing (ExpertSDR3 not running?)"
    else
        ok "CAT sanity: f -> $freq"
    fi

    # audio sidecar -- pick the backend
    if [[ $ENABLE_AUDIO -eq 1 ]]; then
        bold "Starting audio sidecar (backend: $AUDIO_BACKEND)"
        if [[ $AUDIO_BACKEND == pulseaudio ]]; then
            nohup python3 -u "$AUDIO_SIDECAR" \
                --rigctld-host localhost \
                --rigctld-port "$AUDIO_PORT" \
                --name "$SINK_NAME" \
                --tx-gain-db "$TX_GAIN_DB" \
                --rx-gain-db "$RX_GAIN_DB" \
                </dev/null >"$AUDIO_OUT" 2>"$AUDIO_LOG" &
            disown
            sleep 4

            if ! is_running_audio_pa; then
                fail "audio sidecar failed to start; see $AUDIO_OUT $AUDIO_LOG"
                tail -20 "$AUDIO_OUT" >&2 || true
                tail -20 "$AUDIO_LOG" >&2 || true
                return 1
            fi
            ok "audio sidecar (PulseAudio) pid=$(pgrep -f tci-audio-soundcard-sidecar.py)"

            if pactl list sinks short 2>/dev/null | grep -q "${SINK_NAME}-rx" \
               && pactl list sinks short 2>/dev/null | grep -q "${SINK_NAME}-tx"; then
                ok "PulseAudio sinks created: ${SINK_NAME}-rx, ${SINK_NAME}-tx"
            else
                warn "expected ${SINK_NAME}-rx and ${SINK_NAME}-tx sinks not found"
            fi
        else  # AUDIO_BACKEND=gr
            nohup python3 -u "$AUDIO_GR_SIDECAR" \
                --rigctld-host localhost \
                --rigctld-port "$AUDIO_PORT" \
                --zmq-rx-bind "$ZMQ_AUDIO_RX_BIND" \
                --zmq-tx-bind "$ZMQ_AUDIO_TX_BIND" \
                --tx-gain-db "$TX_GAIN_DB" \
                --rx-gain-db "$RX_GAIN_DB" \
                </dev/null >"$AUDIO_OUT" 2>"$AUDIO_LOG" &
            disown
            sleep 3

            if ! is_running_audio_gr; then
                fail "GR audio sidecar failed to start; see $AUDIO_OUT $AUDIO_LOG"
                tail -20 "$AUDIO_OUT" >&2 || true
                tail -20 "$AUDIO_LOG" >&2 || true
                return 1
            fi
            ok "audio sidecar (GR) pid=$(pgrep -f tci-audio-gr-sidecar.py)"
            ok "GR ZMQ RX (audio out): $ZMQ_AUDIO_RX_BIND"
            ok "GR ZMQ TX (audio in):  $ZMQ_AUDIO_TX_BIND"
        fi
    fi

    # IQ sidecar
    if [[ $ENABLE_IQ -eq 1 ]]; then
        bold "Starting IQ sidecar"
        nohup python3 -u "$IQ_SIDECAR" \
            --rigctld-host localhost \
            --rigctld-port "$IQ_PORT" \
            --zmq-bind "$ZMQ_BIND" \
            </dev/null >"$IQ_OUT" 2>"$IQ_LOG" &
        disown
        sleep 2

        if ! is_running_iq; then
            fail "IQ sidecar failed to start; see $IQ_OUT $IQ_LOG"
            tail -20 "$IQ_OUT" >&2 || true
            tail -20 "$IQ_LOG" >&2 || true
            return 1
        fi
        ok "IQ sidecar pid=$(pgrep -f tci-iq-sidecar.py)"
    fi

    echo
    bold "Ready."
    echo "  CAT:           localhost:$RIGCTLD_PORT  (configure JS8Call: 'Hamlib NET rigctl')"
    if [[ $ENABLE_AUDIO -eq 1 ]]; then
        if [[ $AUDIO_BACKEND == pulseaudio ]]; then
            echo "  Audio RX:      ${SINK_NAME}-rx.monitor   (modem audio input)"
            echo "  Audio TX:      ${SINK_NAME}-tx           (modem audio output)"
        else
            echo "  Audio RX:      $ZMQ_AUDIO_RX_BIND  (GR: zmq_sub_source,  type=float)"
            echo "  Audio TX:      $ZMQ_AUDIO_TX_BIND  (GR: zmq_push_sink, type=float)"
            echo "  Audio rate:    8 kHz mono float32"
        fi
    fi
    if [[ $ENABLE_IQ -eq 1 ]]; then
        echo "  IQ stream:     $ZMQ_BIND  (GNU Radio: zmq_sub_source, type=complex float)"
        echo "  IQ rate:       $IQ_RATE Hz"
    fi
    echo "  Stop with:     $0 stop"
}


cmd_stop() {
    bold "Stopping TCI backend"

    # Kill BOTH possible audio backends -- the user may have changed
    # AUDIO_BACKEND between start and stop, and either one might be
    # the actually-running process.
    graceful_kill "tci-iq-sidecar.py"               "IQ sidecar"                 is_running_iq
    graceful_kill "tci-audio-gr-sidecar.py"         "audio sidecar (GR)"         is_running_audio_gr
    graceful_kill "tci-audio-soundcard-sidecar.py"  "audio sidecar (PulseAudio)" is_running_audio_pa
    graceful_kill "rigctld"                         "rigctld"                    is_running_rigctld

    # The PulseAudio sidecar's normal shutdown path already calls
    # 'pactl unload-module' on the sinks it created, but if we had to
    # SIGKILL it, the modules will be orphaned.  Sweep up.
    unload_pa_sinks

    bold "Stopped."
}


cmd_status() {
    bold "TCI backend status"
    if is_running_rigctld; then
        ok "rigctld running pid=$(pgrep -x rigctld)"
    else
        warn "rigctld not running"
    fi

    if is_running_audio_pa; then
        ok "audio sidecar (PulseAudio) running pid=$(pgrep -f tci-audio-soundcard-sidecar.py)"
    elif is_running_audio_gr; then
        ok "audio sidecar (GR) running pid=$(pgrep -f tci-audio-gr-sidecar.py)"
    else
        if [[ $ENABLE_AUDIO -eq 1 ]]; then
            warn "audio sidecar not running (configured backend: $AUDIO_BACKEND)"
        else
            ok "audio sidecar disabled"
        fi
    fi

    if is_running_iq; then
        ok "IQ sidecar running pid=$(pgrep -f tci-iq-sidecar.py)"
    else
        if [[ $ENABLE_IQ -eq 1 ]]; then
            warn "IQ sidecar not running"
        else
            ok "IQ sidecar disabled"
        fi
    fi

    if is_running_audio_pa; then
        echo
        bold "Sinks"
        if pactl list sinks short 2>/dev/null | grep -E "${SINK_NAME}-(rx|tx)\b" | sed 's/^/  /'; then
            :
        else
            warn "no ${SINK_NAME}-rx / ${SINK_NAME}-tx sinks present"
        fi
    fi

    echo
    bold "Listening ports"
    # Include 5555 (IQ ZMQ), 5557 (GR audio RX ZMQ), 5558 (GR audio TX ZMQ).
    ss -tlnp 2>/dev/null | grep -E ":(${RIGCTLD_PORT}|${AUDIO_PORT}|${IQ_PORT}|5555|5557|5558)\b" \
        | sed 's/^/  /' \
        || warn "no rigctld / sidecar ports listening"

    if pgrep -af Expert > /dev/null; then
        echo
        bold "ExpertSDR3"
        pgrep -af Expert | head -3 | sed 's/^/  /'
    else
        echo
        warn "ExpertSDR3 not running -- start it before 'tci.sh start'"
    fi

    if [[ -f $AUDIO_OUT ]]; then
        echo
        bold "Last 6 audio sidecar lines"
        tail -6 "$AUDIO_OUT" | sed 's/^/  /'
    fi

    if [[ -f $IQ_OUT ]]; then
        echo
        bold "Last 6 IQ sidecar lines"
        tail -6 "$IQ_OUT" | sed 's/^/  /'
    fi
}


cmd_log() {
    [[ -f $AUDIO_OUT ]] || die "no audio sidecar log at $AUDIO_OUT"
    exec tail -F "$AUDIO_OUT"
}

cmd_iqlog() {
    [[ -f $IQ_OUT ]] || die "no IQ sidecar log at $IQ_OUT"
    exec tail -F "$IQ_OUT"
}


cmd_help() {
    cat <<EOF
$(bold 'tci.sh') - manage Hamlib TCI rigctld + audio/IQ sidecars

Usage: $0 <command>

Commands:
  start     launch rigctld + enabled sidecars (idempotent; cleans up first)
  stop      kill everything, unload PulseAudio sinks
  restart   stop + start
  status    show running state, sinks, ports, last log lines
  log       tail -F the audio sidecar log ($AUDIO_OUT)
  iqlog     tail -F the IQ    sidecar log ($IQ_OUT)

What you get with current configuration:
  ENABLE_AUDIO=$ENABLE_AUDIO    AUDIO_BACKEND=$AUDIO_BACKEND    ENABLE_IQ=$ENABLE_IQ

EOF

    if [[ $ENABLE_AUDIO -eq 0 && $ENABLE_IQ -eq 0 ]]; then
        cat <<EOF
  → CAT control only (localhost:$RIGCTLD_PORT)
    No audio, no IQ. Configure JS8Call/fldigi to "Hamlib NET rigctl".

EOF
    else
        cat <<EOF
  → CAT control: localhost:$RIGCTLD_PORT
    Configure JS8Call/fldigi/WSJT-X to "Hamlib NET rigctl".

EOF
    fi

    if [[ $ENABLE_AUDIO -eq 1 ]]; then
        if [[ $AUDIO_BACKEND == pulseaudio ]]; then
            cat <<EOF
  → Audio (PulseAudio): tci-rx.monitor (RX), tci-tx (TX)
    USE FOR: JS8Call, fldigi, WSJT-X, any app that uses PulseAudio.
    Configure modem audio input → tci-rx.monitor
    Configure modem audio output → tci-tx

EOF
        else
            cat <<EOF
  → Audio (GNU Radio): ZMQ at $ZMQ_AUDIO_RX_BIND (RX), $ZMQ_AUDIO_TX_BIND (TX)
    USE FOR: GNU Radio flowgraphs processing demodulated audio.
    RX: zmq_sub_source (8 kHz float32 mono) at $ZMQ_AUDIO_RX_BIND
    TX: zmq_push_sink (8 kHz float32 mono) at $ZMQ_AUDIO_TX_BIND

EOF
        fi
    fi

    if [[ $ENABLE_IQ -eq 1 ]]; then
        cat <<EOF
  → IQ stream (GNU Radio): ZMQ at $ZMQ_BIND
    USE FOR: GNU Radio flowgraphs, wideband analysis, custom DSP.
    zmq_sub_source (complex float, $IQ_RATE Hz) at $ZMQ_BIND

EOF
    fi

    cat <<EOF
Common configurations:

  1. Digital modes (JS8Call, fldigi, WSJT-X):
     ENABLE_AUDIO=1  AUDIO_BACKEND=pulseaudio  ENABLE_IQ=0

  2. GNU Radio audio processing (demodulated audio):
     ENABLE_AUDIO=1  AUDIO_BACKEND=gr  ENABLE_IQ=0

  3. GNU Radio IQ processing (raw baseband):
     ENABLE_AUDIO=0  AUDIO_BACKEND=pulseaudio  ENABLE_IQ=1

  4. Both (JS8Call + GNU Radio IQ analysis):
     ENABLE_AUDIO=1  AUDIO_BACKEND=pulseaudio  ENABLE_IQ=1  ← DEFAULT

  5. Both GNU Radio (audio + IQ in same flowgraph):
     ENABLE_AUDIO=1  AUDIO_BACKEND=gr  ENABLE_IQ=1

What each sidecar does:

  Audio sidecar:
    - Streams demodulated RX audio (SSB/AM/FM/CW) from radio
    - Accepts TX audio to modulate and transmit
    - Two backends:
      • pulseaudio: creates tci-rx/tci-tx sinks for JS8Call/fldigi/etc
      • gr: ZMQ endpoints for GNU Radio flowgraphs (8 kHz audio rate)

  IQ sidecar:
    - Streams raw RX IQ samples (baseband, not demodulated)
    - Always uses GNU Radio ZMQ (complex float)
    - Rates: 48/96/192/384 kHz (set IQ_RATE below)
    - RX ONLY (no TX IQ capability)

Configuration (edit the variables at the top of $0):

  Enable/disable:
    ENABLE_AUDIO    $ENABLE_AUDIO  (1=run audio sidecar, 0=skip)
    AUDIO_BACKEND   $AUDIO_BACKEND  (pulseaudio=apps like JS8Call | gr=GNU Radio)
    ENABLE_IQ       $ENABLE_IQ  (1=run IQ sidecar, 0=skip)

  Ports:
    CAT port        $RIGCTLD_PORT  (JS8Call/fldigi configure this)
    audio port      $AUDIO_PORT  (rigctld <-> audio sidecar, internal)
    IQ port         $IQ_PORT  (rigctld <-> IQ sidecar, internal)

  IQ sidecar:
    IQ rate         $IQ_RATE  Hz (48000/96000/192000/384000)
    IQ ZMQ bind     $ZMQ_BIND

  Audio sidecar (GR backend only):
    Audio RX ZMQ    $ZMQ_AUDIO_RX_BIND  (GR: zmq_sub_source, float)
    Audio TX ZMQ    $ZMQ_AUDIO_TX_BIND  (GR: zmq_push_sink, float)

  Gain:
    TX gain         $TX_GAIN_DB dB (boost audio going to radio)
    RX gain         $RX_GAIN_DB dB (boost audio from radio)

Prerequisites:
  - ExpertSDR3 running with TCI enabled on $TCI_HOST:$TCI_PORT
  - rigctld + libhamlib.so.5 + sidecar Python files deployed to /tmp/
  - Edit variables at top of this script, then run: $0 start

Examples:

  # Start with default config (PulseAudio audio + IQ for GNU Radio):
  $0 start

  # Check what's running:
  $0 status

  # Change config for GNU Radio audio instead:
  vim $0  # set AUDIO_BACKEND=gr
  $0 restart

  # Disable IQ, keep audio for JS8Call only:
  vim $0  # set ENABLE_IQ=0
  $0 restart

  # Stop everything:
  $0 stop
EOF
}


# ---------- dispatch ----------

case "${1:-}" in
    start)            cmd_start   ;;
    stop)             cmd_stop    ;;
    restart)          cmd_stop; echo; cmd_start ;;
    status|stat)      cmd_status  ;;
    log|logs|tail)    cmd_log     ;;
    iqlog|iqlogs)     cmd_iqlog   ;;
    help|-h|--help|"") cmd_help   ;;
    *)                die "unknown command: $1 (try '$0 help')" ;;
esac
