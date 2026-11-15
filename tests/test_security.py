import pytest

from renderer.security import is_allowed_supabase_storage_url


@pytest.mark.parametrize("url", [
    "https://project.supabase.co/storage/v1/object/sign/videos/source.mp4?token=abc",
    "https://project-123.supabase.co/storage/v1/object/sign/bucket/path.mp4?token=abc",
    "https://project.supabase.co:443/storage/v1/object/sign/bucket/path.mp4?token=abc",
])
def test_accepts_valid_signed_storage_urls(url):
    assert is_allowed_supabase_storage_url(url)


@pytest.mark.parametrize("url", [
    "http://project.supabase.co/storage/v1/object/sign/bucket/path.mp4?token=abc",
    "https://project.supabase.co:444/storage/v1/object/sign/bucket/path.mp4?token=abc",
    "https://user:pass@project.supabase.co/storage/v1/object/sign/bucket/path.mp4?token=abc",
    "https://project.supabase.co/storage/v1/object/sign/bucket/path.mp4#fragment",
    "https://project.supabase.co/rest/v1/users",
    "https://project.supabase.co.evil.test/storage/v1/object/sign/bucket/path.mp4",
    "https://.supabase.co/storage/v1/object/sign/bucket/path.mp4",
    "https://project.supabase.co/storage/v1/object/sign/",
])
def test_rejects_unsafe_or_non_storage_urls(url):
    assert not is_allowed_supabase_storage_url(url)
