class ClipForgeError(Exception):
    default_error_code = "internal"
    default_user_message = "Something went wrong while processing this VOD."

    def __init__(
        self,
        error_code: str | None = None,
        user_message: str | None = None,
    ) -> None:
        self.error_code = error_code or self.default_error_code
        self.user_message = user_message or self.default_user_message
        super().__init__(self.user_message)


class UnsupportedUrl(ClipForgeError):
    default_error_code = "unsupported_url"
    default_user_message = "Only Twitch VODs and YouTube videos are supported."

    def __init__(self, user_message: str | None = None) -> None:
        super().__init__(self.default_error_code, user_message)


class VodTooLong(ClipForgeError):
    default_error_code = "vod_too_long"
    default_user_message = "VOD is too long (max 240 min in this build)."

    def __init__(self, user_message: str | None = None) -> None:
        super().__init__(self.default_error_code, user_message)


class SubscriberOnly(ClipForgeError):
    default_error_code = "sub_only"
    default_user_message = "This VOD is subscriber-only — ask the streamer or use a public VOD."

    def __init__(self, user_message: str | None = None) -> None:
        super().__init__(self.default_error_code, user_message)


class DownloadFailed(ClipForgeError):
    default_error_code = "download_failed"
    default_user_message = "Could not download this video. It may be private, removed, or blocked."

    def __init__(self, user_message: str | None = None, error_code: str | None = None) -> None:
        super().__init__(error_code or self.default_error_code, user_message)


class TranscriptionFailed(ClipForgeError):
    default_error_code = "transcribe_failed"
    default_user_message = "Transcription failed. Try again in a few minutes."

    def __init__(self, user_message: str | None = None, error_code: str | None = None) -> None:
        super().__init__(error_code or self.default_error_code, user_message)


class NoSpeech(ClipForgeError):
    default_error_code = "no_speech"
    default_user_message = "No speech was detected in this VOD."

    def __init__(self, user_message: str | None = None) -> None:
        super().__init__(self.default_error_code, user_message)


class InvalidStoredKey(ClipForgeError):
    default_error_code = "key_decrypt_failed"
    default_user_message = "The saved API key could not be decrypted. Enter it again."

    def __init__(self, user_message: str | None = None) -> None:
        super().__init__(self.default_error_code, user_message)


class InvalidTransition(Exception):
    def __init__(self, current_status: str, target_status: str) -> None:
        self.current_status = current_status
        self.target_status = target_status
        super().__init__(f"Invalid job transition: {current_status} -> {target_status}")
