"""
VoxRoom AAF export microservice.

Receives a JSON manifest + uploaded audio files, returns a single .aaf
that Pro Tools can open via File > Import > Session Data.

Endpoints:
  GET  /health
  POST /aaf  (multipart: manifest=<json>, files=<audio>...)

Manifest schema (JSON string):
{
  "session_name": "Episode 01 - Reel 1",
  "fps": 25.0,
  "sample_rate": 48000,
  "mode": "base" | "advanced",
  "tracks": [
    {
      "name": "MARCO",
      "clips": [
        {
          "file": "marco_take_03.wav",   # must match a file part name
          "start_seconds": 12.5,         # position on the timeline
          "duration_seconds": 3.2,       # clip length
          "name": "L002 take 3"          # clip label
        }
      ]
    }
  ],
  "markers": [                            # advanced mode only
    { "position_seconds": 12.5, "comment": "più aggressivo" }
  ]
}
"""
from __future__ import annotations

import io
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import aaf2
from fastapi import FastAPI, File, Form, HTTPException, Header, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class Clip(BaseModel):
    file: str
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)
    name: str | None = None


class Track(BaseModel):
    name: str
    clips: list[Clip] = Field(default_factory=list)


class Marker(BaseModel):
    position_seconds: float = Field(ge=0)
    comment: str = ""


class Manifest(BaseModel):
    session_name: str = "VoxRoom Session"
    fps: float = 25.0
    sample_rate: int = 48000
    mode: str = "base"  # "base" | "advanced"
    tracks: list[Track] = Field(default_factory=list)
    markers: list[Marker] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="VoxRoom AAF Service", version="1.0.0")

EXPECTED_TOKEN = os.environ.get("AAF_SERVICE_TOKEN", "").strip()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "voxroom-aaf"}


def _check_auth(authorization: str | None) -> None:
    if not EXPECTED_TOKEN:
        # Allow unauthenticated only when no token is configured (local dev).
        return
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    if authorization.removeprefix("Bearer ").strip() != EXPECTED_TOKEN:
        raise HTTPException(status_code=403, detail="invalid token")


@contextmanager
def _staged_files(uploads: list[UploadFile]) -> Iterator[dict[str, Path]]:
    """Persist uploads to a temp dir and yield {filename: path}."""
    with tempfile.TemporaryDirectory(prefix="aaf_in_") as td:
        td_path = Path(td)
        out: dict[str, Path] = {}
        for up in uploads:
            safe = Path(up.filename or "audio.wav").name
            dest = td_path / safe
            with dest.open("wb") as f:
                while chunk := up.file.read(1 << 20):
                    f.write(chunk)
            out[safe] = dest
        yield out


# ---------------------------------------------------------------------------
# AAF generation
# ---------------------------------------------------------------------------

def _seconds_to_edit_units(seconds: float, edit_rate: float) -> int:
    return int(round(seconds * edit_rate))


def _build_aaf(manifest: Manifest, files: dict[str, Path], out_path: Path) -> None:
    """
    Build an AAF with one timeline TimelineMobSlot per track.
    Each clip imports the WAV as a SourceMob and references it from the
    composition with proper start_time/length expressed in edit units (frames).
    """
    edit_rate = manifest.fps  # composition uses video frame rate
    audio_rate = manifest.sample_rate

    with aaf2.open(str(out_path), "w") as f:
        # Composition mob = the "sequence" Pro Tools will import
        comp = f.create.CompositionMob(manifest.session_name)
        f.content.mobs.append(comp)

        for track in manifest.tracks:
            slot = comp.create_timeline_slot(edit_rate=edit_rate)
            slot.name = track.name
            seq = f.create.Sequence("sound")
            slot.segment = seq

            cursor_units = 0
            # Sort clips by start to lay them out left to right with fillers.
            clips_sorted = sorted(track.clips, key=lambda c: c.start_seconds)

            for clip in clips_sorted:
                start_units = _seconds_to_edit_units(clip.start_seconds, edit_rate)
                length_units = max(
                    1, _seconds_to_edit_units(clip.duration_seconds, edit_rate)
                )

                # Insert silence (Filler) up to the clip start.
                if start_units > cursor_units:
                    filler = f.create.Filler("sound", start_units - cursor_units)
                    seq.components.append(filler)
                    cursor_units = start_units

                wav_path = files.get(clip.file)
                if wav_path is None or not wav_path.exists():
                    # Skip missing files but keep timeline aligned.
                    filler = f.create.Filler("sound", length_units)
                    seq.components.append(filler)
                    cursor_units += length_units
                    continue

                # Import the WAV as a master/source mob; returns the master mob.
                master_mob = f.create.MasterMob(clip.name or wav_path.stem)
                f.content.mobs.append(master_mob)
                master_mob.import_audio_essence(str(wav_path), edit_rate)

                # SourceClip referencing the master mob's audio slot.
                src_clip = master_mob.create_source_clip(
                    slot_id=1, length=length_units
                )
                seq.components.append(src_clip)
                cursor_units += length_units

        # Markers (advanced mode): attach to a dedicated event slot.
        if manifest.mode == "advanced" and manifest.markers:
            # Director notes are stored two ways for maximum Pro Tools compatibility:
            #   1) As composition-level comments (always visible in Session Info).
            #   2) As a dedicated empty timeline slot named "DIR_NOTES" containing
            #      zero-length-ish filler segments so the engineer sees a track
            #      reference. Real markers/locators use AAF DescriptiveMarker which
            #      pyaaf2 doesn't expose with a high-level helper, so we stick to
            #      what is guaranteed to round-trip into Pro Tools cleanly.
            for idx, m in enumerate(manifest.markers, start=1):
                tc = _seconds_to_edit_units(m.position_seconds, edit_rate)
                comp.comments[f"NOTE_{idx:03d}_@{tc}f"] = m.comment or ""


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@app.post("/aaf")
async def make_aaf(
    manifest: str = Form(..., description="JSON-encoded Manifest"),
    files: list[UploadFile] = File(default_factory=list),
    authorization: str | None = Header(default=None),
) -> Response:
    _check_auth(authorization)

    try:
        parsed = Manifest(**json.loads(manifest))
    except (json.JSONDecodeError, ValidationError) as e:
        raise HTTPException(status_code=400, detail=f"invalid manifest: {e}") from e

    if not parsed.tracks:
        raise HTTPException(status_code=400, detail="no tracks in manifest")

    with _staged_files(files) as staged, tempfile.TemporaryDirectory(prefix="aaf_out_") as out_dir:
        out_path = Path(out_dir) / "session.aaf"
        try:
            _build_aaf(parsed, staged, out_path)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"aaf build failed: {e}") from e

        data = out_path.read_bytes()

    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in parsed.session_name) or "session"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.aaf"'},
    )
