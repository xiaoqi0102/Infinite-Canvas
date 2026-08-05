"""视频接口插件包。"""

from .common import (
    UnsafePublicUrlError,
    canonical_video_api_root,
    humanize_video_task_failure,
    public_http_get,
    public_http_probe,
    resolve_video_download_url,
    submit_http_request_with_logging,
    submit_video_http_request,
)
from .aicost import (
    AICOST_VIDEO_REQUEST_MODE,
    AICostProtocolError,
    generate_aicost_video,
    is_aicost_official_provider,
    resume_aicost_video,
)
from .geeknow import (
    GEEKNOW_VIDEO_REQUEST_MODE,
    GeekNowProtocolError,
    generate_geeknow_video,
    is_geeknow_official_provider,
    resume_geeknow_video,
)
from .megabyai import (
    MEGABYAI_VIDEO_REQUEST_MODE,
    MegabyAIProtocolError,
    generate_megabyai_video,
    is_megabyai_official_provider,
    megabyai_video_task_retryable,
    resume_megabyai_video,
)
from .meai import (
    MEAI_VIDEO_REQUEST_MODE,
    MeAIProtocolError,
    generate_meai_video,
    is_meai_official_provider,
    resume_meai_video,
)
from .sudashui import (
    SUDASHUI_VIDEO_REQUEST_MODE,
    SudashuiProtocolError,
    generate_sudashui_video,
    is_sudashui_official_base_url,
    resume_sudashui_video,
    sudashui_video_task_pending,
    upload_sudashui_media,
)

__all__ = [
    "humanize_video_task_failure",
    "canonical_video_api_root",
    "public_http_get",
    "public_http_probe",
    "resolve_video_download_url",
    "submit_http_request_with_logging",
    "submit_video_http_request",
    "UnsafePublicUrlError",
    "AICOST_VIDEO_REQUEST_MODE",
    "AICostProtocolError",
    "generate_aicost_video",
    "is_aicost_official_provider",
    "resume_aicost_video",
    "GEEKNOW_VIDEO_REQUEST_MODE",
    "GeekNowProtocolError",
    "generate_geeknow_video",
    "is_geeknow_official_provider",
    "resume_geeknow_video",
    "MEGABYAI_VIDEO_REQUEST_MODE",
    "MegabyAIProtocolError",
    "generate_megabyai_video",
    "is_megabyai_official_provider",
    "megabyai_video_task_retryable",
    "resume_megabyai_video",
    "MEAI_VIDEO_REQUEST_MODE",
    "MeAIProtocolError",
    "generate_meai_video",
    "is_meai_official_provider",
    "resume_meai_video",
    "SUDASHUI_VIDEO_REQUEST_MODE",
    "SudashuiProtocolError",
    "generate_sudashui_video",
    "is_sudashui_official_base_url",
    "resume_sudashui_video",
    "sudashui_video_task_pending",
    "upload_sudashui_media",
]
