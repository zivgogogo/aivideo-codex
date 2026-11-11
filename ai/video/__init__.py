"""HappyHorse/Wan video primitives. Importing does not load keys or use the network."""

from .client import (
    DashScopeAPIError,
    DashScopeError,
    DashScopeTaskError,
    DashScopeTimeoutError,
    DashScopeTransportError,
    DashScopeVideoClient,
    download_video,
)
from .payloads import (
    EDIT_MODEL,
    I2V_MODEL,
    R2V_MODEL,
    T2V_MODEL,
    WAN_MODEL,
    WAN_REFERENCE_MODEL,
    build_frame_video_payload,
    build_image_video_payload,
    build_reference_video_payload,
    build_text_video_payload,
    build_video_edit_payload,
    build_multimodal_reference_payload,
)


def text_to_video(prompt: str, **parameters) -> str:
    return DashScopeVideoClient().text_to_video(prompt, **parameters)


def image_to_video(prompt: str = "", image_path=None, **parameters) -> str:
    return DashScopeVideoClient().image_to_video(prompt, image_path, **parameters)


def reference_to_video(prompt: str, reference_images, **parameters) -> str:
    return DashScopeVideoClient().reference_to_video(
        prompt, reference_images, **parameters
    )


def edit_video(prompt: str, video_url: str, **parameters) -> str:
    return DashScopeVideoClient().edit_video(prompt, video_url, **parameters)


def first_last_frame_to_video(
    prompt: str, first_frame, last_frame=None, **parameters
) -> str:
    return DashScopeVideoClient().first_last_frame_to_video(
        prompt, first_frame, last_frame, **parameters
    )
