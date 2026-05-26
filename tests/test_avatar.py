from __future__ import annotations

import pytest


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\xf8\x0f\x00\x01\x01\x01\x00\x18\xdd\x8d\xb0"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


def test_fnv1a_stable(fake_home):
    import forge_avatar

    assert forge_avatar.fnv1a("") == 0x811C9DC5
    assert forge_avatar.fnv1a("designer") == forge_avatar.fnv1a("designer")
    assert forge_avatar.fnv1a("designer") != forge_avatar.fnv1a("judy")


def test_identity_patch_replaces_existing_avatar(fake_home):
    import forge_avatar

    text = "# IDENTITY.md\n\n- **Name:** Dora\n- **Emoji:** 🎨\n- **Avatar:** /old/avatar.png\n"
    out = forge_avatar.patch_identity_avatar(text, "/Users/symbolstar/.openclaw/workspace-dora/avatar.png")
    assert out.count("- **Avatar:**") == 1
    assert "- **Avatar:** /Users/symbolstar/.openclaw/workspace-dora/avatar.png" in out
    assert "/old/avatar.png" not in out


def test_identity_patch_inserts_after_emoji(fake_home):
    import forge_avatar

    text = "# IDENTITY.md\n\n- **Name:** Dora\n- **Emoji:** 🎨\n- **Role:** Designer\n"
    out = forge_avatar.patch_identity_avatar(text, "/abs/avatar.png")
    assert out.splitlines()[4] == "- **Avatar:** /abs/avatar.png"
    assert out.splitlines()[5] == "- **Role:** Designer"


def test_magic_bytes_sniff_before_extension(fake_home):
    import forge_avatar

    assert forge_avatar.sniff_image_type(PNG_1X1) == "image/png"
    assert forge_avatar.sniff_image_type(b"\xff\xd8\xff\xe0not really jpg") == "image/jpeg"
    assert forge_avatar.sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPxxxx") == "image/webp"
    assert forge_avatar.sniff_image_type(b"avatar.png") is None


def test_atomic_identity_failure_rolls_back_avatar(fake_home, monkeypatch):
    import forge_avatar

    ws = fake_home / ".openclaw" / "workspace-judy"
    ws.mkdir(parents=True)
    path = ws / "avatar.png"
    old = b"\x89PNG\r\n\x1a\nold"
    path.write_bytes(old)

    def boom(agent_id, avatar_abs_path):
        raise RuntimeError("identity write failed")

    monkeypatch.setattr(forge_avatar, "sync_identity_avatar", boom)
    with pytest.raises(RuntimeError):
        forge_avatar.save_avatar("judy", PNG_1X1)
    assert path.read_bytes() == old


def test_save_avatar_writes_png_and_identity_then_delete(fake_home):
    import forge_avatar

    body = forge_avatar.save_avatar("judy", PNG_1X1)
    assert body["agent"] == "judy"
    assert body["url"].endswith(str(body["mtime_ms"]))
    ident = fake_home / ".openclaw" / "workspace-judy" / "IDENTITY.md"
    assert "- **Avatar:** " in ident.read_text(encoding="utf-8")
    assert (fake_home / ".openclaw" / "workspace-judy" / "avatar.png").read_bytes() == PNG_1X1

    body = forge_avatar.delete_avatar("judy")
    assert body["deleted"] is True
    assert not (fake_home / ".openclaw" / "workspace-judy" / "avatar.png").exists()
    assert "- **Avatar:**" not in ident.read_text(encoding="utf-8")


def test_save_avatar_rejects_unknown_magic(fake_home):
    import forge_avatar

    with pytest.raises(forge_avatar.UnsupportedAvatarError):
        forge_avatar.save_avatar("judy", b"not an image")
