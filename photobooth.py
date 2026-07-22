"""
Photobooth - welcome screen, live preview, countdown, capture, and print.

The touchscreen triggers photos. The GPIO3 gpio-shutdown overlay handles the
arcade power button outside of this script.
"""

import os
import time
from datetime import datetime
from pathlib import Path

os.environ.setdefault("SDL_VIDEODRIVER", "wayland")

import pygame
from escpos.printer import File as EscposFile
from libcamera import Transform
from picamera2 import Picamera2
from PIL import Image


# --- Configuration ---

SCREEN_W, SCREEN_H = 800, 480
SQUARE_SIZE = 480
SQUARE_X = (SCREEN_W - SQUARE_SIZE) // 2
SQUARE_Y = 0

CAMERA_RES = (800, 480)
CAPTURE_RES = (1920, 1080)
COUNTDOWN_SECONDS = 3

PRINTER_DEVICE = "/dev/usb/lp0"
PRINTER_WIDTH_PX = 384

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"
PHOTOS_DIR = BASE_DIR / "captures"

BACKGROUND_FILE = ASSETS_DIR / "screen.png"
FONT_FILE = ASSETS_DIR / "BERKY.ttf"

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
PINK = (255, 182, 193)

STATE_WELCOME = "welcome"
STATE_PREVIEW = "preview"
STATE_COUNTDOWN = "countdown"
STATE_PRINTING = "printing"


def frame_to_square_surface(frame):
    """Center-crop an 800x480 RGB frame to 480x480."""
    crop_x_start = (frame.shape[1] - SQUARE_SIZE) // 2
    cropped = frame[
        :,
        crop_x_start:crop_x_start + SQUARE_SIZE,
        :
    ]

    # Picamera2 supplies RGB888, while pygame.surfarray expects the axes swapped.
    return pygame.surfarray.make_surface(cropped.swapaxes(0, 1))


def crop_pygame_image_to_square(surface):
    """Center-crop a pygame surface to a square and scale it for the display."""
    width, height = surface.get_size()
    size = min(width, height)
    left = (width - size) // 2
    top = (height - size) // 2

    cropped = pygame.Surface((size, size))
    cropped.blit(
        surface,
        (0, 0),
        area=pygame.Rect(left, top, size, size),
    )
    return pygame.transform.scale(cropped, (SQUARE_SIZE, SQUARE_SIZE))


def draw_text_top(screen, text, font, color, y=20):
    """Draw centered text with a dark outline so it stays visible."""
    text_surface = font.render(text, True, color)
    outline_surface = font.render(text, True, BLACK)

    rect = text_surface.get_rect(
        center=(SCREEN_W // 2, y + text_surface.get_height() // 2)
    )

    for offset_x, offset_y in (
        (-4, 0),
        (4, 0),
        (0, -4),
        (0, 4),
        (-3, -3),
        (3, -3),
        (-3, 3),
        (3, 3),
    ):
        screen.blit(outline_surface, rect.move(offset_x, offset_y))

    screen.blit(text_surface, rect)


def draw_countdown(screen, text, font):
    """Center countdown glyphs using their visible pixel bounds."""
    text_surface = font.render(text, True, WHITE)
    outline_surface = font.render(text, True, BLACK)

    bounds = text_surface.get_bounding_rect()

    x = SCREEN_W // 2 - bounds.width // 2 - bounds.x
    y = SCREEN_H // 2 - bounds.height // 2 - bounds.y

    for offset_x, offset_y in (
        (-5, 0),
        (5, 0),
        (0, -5),
        (0, 5),
        (-4, -4),
        (4, -4),
        (-4, 4),
        (4, 4),
    ):
        screen.blit(outline_surface, (x + offset_x, y + offset_y))

    screen.blit(text_surface, (x, y))


def print_photo(image_path):
    """Crop, resize, rotate, and print one photo."""
    printer = EscposFile(PRINTER_DEVICE)

    try:
        printer._raw(b"\x1b\x40")
        time.sleep(0.1)

        printer._raw(b"\x12\x23\x0A")
        printer._raw(b"\x1b\x37\x07\x50\x02")
        time.sleep(0.1)

        with Image.open(image_path) as source:
            image = source.copy()

        size = min(image.width, image.height)
        left = (image.width - size) // 2
        top = (image.height - size) // 2

        image = image.crop((left, top, left + size, top + size))
        image = image.resize(
            (PRINTER_WIDTH_PX, PRINTER_WIDTH_PX),
            Image.Resampling.LANCZOS,
        )

        # Keep this rotation if the printer physically outputs photos upside-down.
        image = image.rotate(180)
        image = image.convert("L")

        printer.image(image, impl="bitImageRaster")
        printer.text("\n\n\n\n")
        time.sleep(2)

    finally:
        printer.close()


def main():
    PHOTOS_DIR.mkdir(parents=True, exist_ok=True)

    # hflip=True reverses the existing mirror effect for both the live preview
    # and the saved/printed photo.
    camera_transform = Transform(hflip=True)

    picam2 = Picamera2()

    preview_config = picam2.create_preview_configuration(
        main={"size": CAMERA_RES, "format": "RGB888"},
        transform=camera_transform,
    )

    capture_config = picam2.create_still_configuration(
        main={"size": CAPTURE_RES},
        transform=camera_transform,
    )

    picam2.configure(preview_config)
    picam2.start()

    pygame.init()
    screen = pygame.display.set_mode(
        (SCREEN_W, SCREEN_H),
        pygame.FULLSCREEN | pygame.NOFRAME,
    )
    pygame.display.set_caption("Photobooth")
    pygame.mouse.set_visible(False)

    background = pygame.image.load(str(BACKGROUND_FILE)).convert()
    if background.get_size() != (SCREEN_W, SCREEN_H):
        background = pygame.transform.scale(
            background,
            (SCREEN_W, SCREEN_H),
        )

    # Use the custom font for both the countdown and printing text.
    # Countdown positioning uses visible glyph bounds so unusual font metrics
    # do not push the numbers off-screen.
    font_countdown = pygame.font.Font(str(FONT_FILE), 170)

    try:
        font_printing = pygame.font.Font(str(FONT_FILE), 60)
    except (FileNotFoundError, pygame.error) as error:
        print(f"Custom font unavailable; using default font: {error}", flush=True)
        font_printing = pygame.font.Font(None, 60)

    debounce_seconds = 0.4
    clock = pygame.time.Clock()

    state = STATE_WELCOME
    state_start = time.time()
    last_capture_path = None

    print(
        "Photobooth running. Tap the screen to take a photo. "
        "Press ESC or Q to quit.",
        flush=True,
    )

    running = True

    try:
        while running:
            now = time.time()
            elapsed = now - state_start
            triggered = False

            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False

                elif event.type == pygame.KEYDOWN:
                    if event.key in (pygame.K_ESCAPE, pygame.K_q):
                        running = False

                elif event.type in (
                    pygame.MOUSEBUTTONDOWN,
                    pygame.FINGERDOWN,
                ):
                    triggered = True

            if elapsed < debounce_seconds:
                triggered = False

            if state == STATE_WELCOME:
                screen.blit(background, (0, 0))
                if triggered:
                    state = STATE_PREVIEW
                    state_start = now

            elif state == STATE_PREVIEW:
                screen.fill(PINK)

                frame = picam2.capture_array("main")
                screen.blit(
                    frame_to_square_surface(frame),
                    (SQUARE_X, SQUARE_Y),
                )

                if triggered:
                    state = STATE_COUNTDOWN
                    state_start = now

            elif state == STATE_COUNTDOWN:
                screen.fill(PINK)

                frame = picam2.capture_array("main")
                screen.blit(
                    frame_to_square_surface(frame),
                    (SQUARE_X, SQUARE_Y),
                )

                remaining = COUNTDOWN_SECONDS - int(elapsed)

                if remaining > 0:
                    draw_countdown(
                        screen,
                        str(remaining),
                        font_countdown,
                    )
                else:
                    screen.fill(WHITE)
                    pygame.display.flip()
                    pygame.time.wait(80)

                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    last_capture_path = (
                        PHOTOS_DIR / f"photo_{timestamp}.jpg"
                    )

                    try:
                        # Picamera2 switches to still mode, captures the file,
                        # and switches back to the preview configuration.
                        picam2.switch_mode_and_capture_file(
                            capture_config,
                            str(last_capture_path),
                        )

                        state = STATE_PRINTING
                        state_start = time.time()

                    except Exception as error:
                        print(f"Capture error: {error}", flush=True)
                        state = STATE_PREVIEW
                        state_start = time.time()

            elif state == STATE_PRINTING:
                screen.fill(PINK)

                if last_capture_path and last_capture_path.exists():
                    photo = pygame.image.load(str(last_capture_path))
                    photo = crop_pygame_image_to_square(photo)
                    screen.blit(photo, (SQUARE_X, SQUARE_Y))

                draw_text_top(
                    screen,
                    "Printing...",
                    font_printing,
                    WHITE,
                    y=15,
                )

                if elapsed < 0.1:
                    pygame.display.flip()

                    try:
                        if last_capture_path:
                            print_photo(last_capture_path)
                    except Exception as error:
                        print(f"Print error: {error}", flush=True)

                if elapsed >= 5.0:
                    state = STATE_WELCOME
                    state_start = time.time()

            pygame.display.flip()
            clock.tick(30)

    finally:
        picam2.stop()
        pygame.quit()
        print("Photobooth stopped.", flush=True)


if __name__ == "__main__":
    main()
