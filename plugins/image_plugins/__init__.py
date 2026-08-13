"""图片接口插件包。"""

from .aicost import (
    AICOST_IMAGE_OFFICIAL_HOSTNAMES,
    AICOST_IMAGE_REQUEST_MODE,
    AICostImageProtocolError,
    generate_aicost_image,
    is_aicost_image_official_provider,
    query_aicost_image_task,
)
from .qiniu import (
    QINIU_IMAGE_OFFICIAL_HOSTNAMES,
    QINIU_IMAGE_REQUEST_MODE,
    QiniuImageProtocolError,
    generate_qiniu_image,
    is_qiniu_image_official_provider,
    query_qiniu_image_task,
)

__all__ = [
    "AICOST_IMAGE_OFFICIAL_HOSTNAMES",
    "AICOST_IMAGE_REQUEST_MODE",
    "AICostImageProtocolError",
    "generate_aicost_image",
    "is_aicost_image_official_provider",
    "query_aicost_image_task",
    "QINIU_IMAGE_OFFICIAL_HOSTNAMES",
    "QINIU_IMAGE_REQUEST_MODE",
    "QiniuImageProtocolError",
    "generate_qiniu_image",
    "is_qiniu_image_official_provider",
    "query_qiniu_image_task",
]
