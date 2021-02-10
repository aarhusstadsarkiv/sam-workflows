import json
from pathlib import Path
from typing import List, Any, Dict, Optional

import fitz
from PIL import Image, UnidentifiedImageError, ExifTags

from ..helpers import (
    load_csv_from_sam,
    save_csv_to_sam,
    add_watermark,
    upload_files,
)
from ..settings import (
    SAM_WATERMARK_WIDTH,
    SAM_ACCESS_LARGE_SIZE,
    SAM_ACCESS_LARGE_PATH,
    SAM_ACCESS_MEDIUM_SIZE,
    SAM_ACCESS_MEDIUM_PATH,
    SAM_ACCESS_SMALL_SIZE,
    SAM_ACCESS_SMALL_PATH,
    SAM_ACCESS_PATH,
    SAM_MASTER_PATH,
    SAM_IMAGE_FORMATS,
)

# -----------------------------------------------------------------------------
# Classes
# -----------------------------------------------------------------------------


class PDFConvertError(Exception):
    """Implements error to raise when pdf-conversion fails."""


class ImageConvertError(Exception):
    """Implements error to raise when image-conversion fails."""


# -----------------------------------------------------------------------------
# Functions
# -----------------------------------------------------------------------------
def _convert_pdf_cover_to_image(pdf_in: Path, out_folder: Path) -> Path:
    doc = fitz.open(pdf_in)
    page = doc.loadPage(0)
    pix = page.getPixmap()
    out_file = str(out_folder / pdf_in.stem) + ".png"
    try:
        pix.writePNG(out_file)
        return Path(out_file)
    except Exception as e:
        raise PDFConvertError(e)


def _generate_jpgs(
    img_in: Path,
    quality: int = 80,
    sizes: List = [1920, 640, 150],
    watermark: bool = False,
    overwrite: bool = False,
) -> Dict:

    resp = {}
    try:
        img: Any = Image.open(img_in)
    except UnidentifiedImageError:
        resp["error"] = f"Failed to open {img_in} as an image."
    except Exception as e:
        resp["error"] = f"Error opening file {img_in.name}: {e}"
    else:
        img.load()

        # JPG image might be rotated. Fix, if rotatet.
        if hasattr(img, "_getexif"):  # only present in JPGs
            # Find the orientation exif tag.
            orientation_key: Optional[int] = None
            for tag, tag_value in ExifTags.TAGS.items():
                if tag_value == "Orientation":
                    orientation_key = tag
                    break

            # If exif data is present, rotate image according to
            # orientation value.
            if img.getexif() is not None:
                exif: Dict[Any, Any] = dict(img.getexif().items())
                orientation: Optional[int] = exif.get(orientation_key)
                if orientation == 3:
                    img = img.rotate(180)
                elif orientation == 6:
                    img = img.rotate(270)
                elif orientation == 8:
                    img = img.rotate(90)

        for size in sizes:
            copy_img = img.copy()
            # .thumbnail() doesn't enlarge smaller img and keeps aspect-ratio
            copy_img.thumbnail((size, size))

            # If larger than watermark-width, add watermark
            if watermark and (copy_img.width > SAM_WATERMARK_WIDTH):
                copy_img = add_watermark(copy_img)

            # If not rbg, convert before saving as jpg
            if copy_img.mode != "RGB":
                copy_img = copy_img.convert("RGB")

            access_dirs = {
                SAM_ACCESS_LARGE_SIZE: SAM_ACCESS_LARGE_PATH,
                SAM_ACCESS_MEDIUM_SIZE: SAM_ACCESS_MEDIUM_PATH,
                SAM_ACCESS_SMALL_SIZE: SAM_ACCESS_SMALL_PATH,
            }

            out_dir = access_dirs.get(size) or SAM_ACCESS_PATH
            new_filename = img_in.stem + ".jpg"
            out_file = Path(out_dir, new_filename)

            # Skip saving, if overwrite is False and file already exists
            if (not overwrite) and out_file.exists():
                resp["error"] = f"File already exists: {out_file}"
                continue
            # Save file to local directory
            try:
                copy_img.save(
                    out_file,
                    "JPEG",
                    quality=quality,
                )
                resp[size] = str(out_file)
            except Exception as e:
                resp["error"] = f"Error saving file {copy_img.filename}: {e}"
    return resp


async def generate_sam_access_files(
    csv_in: Path,
    csv_out: Path,
    watermark: bool = False,
    upload: bool = False,
    overwrite: bool = False,
) -> None:
    """Generates, uploads and copies access-images from the files in the
    csv-file.

    Parameters
    ----------
    csv_in : Path
        Csv-file from SAM csv-export
    csv_out: Path
        Csv-file to import into SAM
    watermark: bool
        Watermark access-files. Defaults to False
    upload: bool
        Upload the generated access-files to Azure. Defaults to False
    overwrite: bool
        Overwrite existing files in both local storage and Azure. Defaults to
        False

    Raises
    ------
    PDFConvertError
        Raised when errors in conversion occur. Errors from PIL are caught
        and re-raised with this error. If no pdf-files are loaded, this error
        is raised as well.
    ImageConvertError
        Raised when errors in conversion occur. Errors from PIL are caught
        and re-raised with this error. If no pdf-files are loaded, this error
        is raised as well.
    """

    # Load SAM-csv with rows of file-references
    try:
        files: List[Dict] = load_csv_from_sam(csv_in)
        print("Csv-file loaded...", flush=True)
    except Exception as e:
        raise e

    output: List[Dict] = []
    converted: int = 0
    skipped: int = 0
    failed: int = 0
    uploaded: int = 0

    # Generate access-files
    for file in files:
        # Load SAM-data
        data = json.loads(file["oasDataJsonEncoded"])
        legal_status: str = data.get("other_restrictions", "4")
        constractual_status: str = data.get("contractual_status", "1")
        filename: str = data["filename"]

        # Check rights
        if int(legal_status.split(";")[0]) > 1:
            print(f"Skipping {filename} due to legal restrictions", flush=True)
            skipped += 1
            continue
        if int(constractual_status.split(";")[0]) < 3:
            print(
                f"Skipping {filename} due to contractual restrictions",
                flush=True,
            )
            skipped += 1
            continue

        # Check filepath
        filepath = Path(SAM_MASTER_PATH, filename)
        if not filepath.exists():
            print(f"No file found at: {filepath}", flush=True)
            failed += 1
            continue
        if not filepath.is_file():
            print(f"Filepath refers to a directory: {filepath}", flush=True)
            failed += 1
            continue

        # Determine fileformat
        if filepath.suffix in SAM_IMAGE_FORMATS:
            filesizes = [
                SAM_ACCESS_LARGE_SIZE,
                SAM_ACCESS_MEDIUM_SIZE,
                SAM_ACCESS_SMALL_SIZE,
            ]
            record_type = "image"
        elif filepath.suffix == ".pdf":
            filesizes = [SAM_ACCESS_MEDIUM_SIZE, SAM_ACCESS_SMALL_SIZE]
            filepath = _convert_pdf_cover_to_image(
                filepath, Path("./images/temp")
            )
            record_type = "web_document"
        else:
            print(f"Unknown fileformat for {filepath.name}", flush=True)
            skipped += 1
            continue

        # Generate access-files
        convert_resp = _generate_jpgs(
            filepath, sizes=filesizes, watermark=watermark
        )

        # Check response from convert-function
        if convert_resp.get("error"):
            print(convert_resp["error"], flush=True)
            failed += 1
            continue

        print(f"Successfully converted {filename}", flush=True)
        converted += 1

        small = convert_resp.get(SAM_ACCESS_SMALL_SIZE)
        medium = convert_resp.get(SAM_ACCESS_MEDIUM_SIZE)
        large = convert_resp.get(SAM_ACCESS_LARGE_SIZE)

        # Upload access-files
        if upload:
            filepaths = [Path(small), Path(medium)]
            if large:
                filepaths.append(Path(large))
            try:
                urls = await upload_files(filepaths, overwrite=overwrite)
                small = urls[0]
                medium = urls[1]
                large = urls[2]
                print(
                    f"Successfully uploaded accessfiles for {filename}",
                    flush=True,
                )
                uploaded += 1
            except Exception as e:
                print(
                    f"Failed to upload accessfiles for {filename}. Error: {e}",
                    flush=True,
                )
                failed += 1
                continue

        output.append(
            {
                "oasid": filepath.stem,
                "thumbnail": small,
                "record_image": medium,
                "record_type": record_type,
                "large_image": large or "",
                "web_document_url": "web_url",
            }
        )
    if output:
        save_csv_to_sam(output, csv_out)

    print("Done", flush=True)
    print(
        f"Converted: {converted}, uploaded: {uploaded}, failed: {failed}, \
        skipped: {skipped}",
        flush=True,
    )
