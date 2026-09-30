"""DanPASS operator status while protected sound-file access is pending."""

from __future__ import annotations

import json
from pathlib import Path


DATASET_STATUS = {
    "dataset": "danpass-dialogue",
    "dataset_class": "PRIMARY-INDEPENDENTISH",
    "status": "PENDING_ACCESS",
    "source_url": "https://www.danpass.hum.ku.dk/",
    "source_license": "Non-commercial use with attribution",
    "access": {
        "reason": "Opening the DanPASS sound archives requires a password.",
        "contact": "ninag@hum.ku.dk",
        "terms": "Request the password from the corpus owner. Use is limited to non-commercial purposes and requires attribution.",
        "next_steps": [
            "Request the sound-file password from the corpus contact.",
            "Download the dialogue stereo audio, paired speaker mono audio, and Praat TextGrid archives through the official page.",
            "Resume preparation from those local files after access is available.",
        ],
    },
    "planned_views": ["dialogue stereo mixed audio", "oracle mono speaker channels"],
}


def write_pending_access_status(output_dir: Path) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    status_path = output_dir / "dataset-status.json"
    status_path.write_text(
        json.dumps(DATASET_STATUS, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return status_path
