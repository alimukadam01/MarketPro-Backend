"""
Turning an upload into images: separate photos, a folder (the browser sends
its files one by one) or a zip, unpacked here.

Everything is checked as an image by opening it, not by its name. What is
not an image - a PDF, a text file, a zip inside the zip - is skipped and
counted. The zip itself is never stored (§4: "zips are deleted after
unpacking"): it is read from the upload and dropped.

Caps keep a hostile or broken zip from filling the disk or memory.
"""
import io
import os
import zipfile

from PIL import Image

MAX_IMAGES = 1000
MAX_IMAGE_BYTES = 25 * 1024 * 1024          # one photo
MAX_ZIP_TOTAL_BYTES = 1024 * 1024 * 1024    # everything a zip unpacks to
MAX_ZIP_ENTRIES = 3000


class UploadTooLarge(Exception):
    pass


def is_image(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        return True
    except Exception:
        return False


def _clean_name(name):
    # Paths inside a zip ("../../x.jpg", "Book/p1.jpg") are never used as
    # paths: only the file's own name is kept.
    base = os.path.basename(name.replace('\\', '/'))
    return base or 'page.jpg'


def _from_zip(upload):
    images, skipped = [], 0
    total = 0
    with zipfile.ZipFile(upload) as archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        if len(members) > MAX_ZIP_ENTRIES:
            raise UploadTooLarge(f'The zip has more than {MAX_ZIP_ENTRIES} files.')
        for info in members:
            if info.file_size > MAX_IMAGE_BYTES:
                skipped += 1
                continue
            total += info.file_size
            if total > MAX_ZIP_TOTAL_BYTES:
                raise UploadTooLarge('The zip unpacks to more than 1 GB.')
            data = archive.read(info)
            # The declared size is the archive's word; trust only what came out.
            if len(data) > MAX_IMAGE_BYTES:
                skipped += 1
                continue
            if zipfile.is_zipfile(io.BytesIO(data)) or not is_image(data):
                skipped += 1
                continue
            images.append((_clean_name(info.filename), data))
    return images, skipped


def extract_images(uploads):
    """[(name, bytes)] for every image in the uploaded files, and how many
    files were skipped."""
    images, skipped = [], 0
    for upload in uploads:
        if zipfile.is_zipfile(upload):
            upload.seek(0)
            found, missed = _from_zip(upload)
            images.extend(found)
            skipped += missed
        else:
            upload.seek(0)
            data = upload.read()
            if len(data) <= MAX_IMAGE_BYTES and is_image(data):
                images.append((_clean_name(upload.name), data))
            else:
                skipped += 1
        if len(images) > MAX_IMAGES:
            raise UploadTooLarge(f'One upload can hold at most {MAX_IMAGES} pages.')
    return images, skipped
