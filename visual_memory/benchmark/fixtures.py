from __future__ import annotations

from dataclasses import dataclass

from PIL import Image, ImageDraw


@dataclass(frozen=True)
class BenchmarkCase:
    label: str
    profile: str
    query: str
    description: str
    code: str
    expected_change: str
    image: Image.Image


def _screen(label: str, profile: str, description: str, accent: str, state: str) -> Image.Image:
    image = Image.new("RGB", (480, 270), "#f4f6f8" if profile == "website" else "#151b25")
    draw = ImageDraw.Draw(image)
    if profile == "website":
        draw.rectangle((0, 0, 479, 39), fill="#202b3c")
        draw.text((16, 13), "AI STACK DASHBOARD", fill="white")
        draw.rectangle((18, 58, 462, 252), fill="white", outline="#d9e0e8")
        draw.text((34, 73), description.upper(), fill="#1f2937")
        if state == "nav_overlap":
            draw.rectangle((18, 55, 462, 98), fill="#dceaff")
            draw.text((34, 63), "MENU  MODELS  STATUS", fill="#143d72")
            draw.text((34, 84), "SYSTEM OVERVIEW", fill="#1f2937")
        elif state == "error_banner":
            draw.rectangle((34, 112, 442, 153), fill="#d52b35")
            draw.text((47, 127), "ERROR  Gateway connection failed", fill="white")
        elif state == "gpu_graph":
            draw.text((34, 111), "GPU UTILIZATION", fill="#334155")
            draw.rectangle((42, 145, 430, 221), outline="#aab4c2")
            for x, height in ((62, 25), (108, 42), (154, 34), (200, 60), (246, 50), (292, 70), (338, 56), (384, 65)):
                draw.rectangle((x, 217 - height, x + 20, 217), fill="#3378ca")
        elif state == "voice_panel":
            draw.rectangle((34, 111, 442, 231), fill="#edf4fc", outline="#88a5c6")
            draw.ellipse((62, 144, 112, 194), fill="#26885d")
            draw.text((129, 143), "VOICE SESSION  Listening", fill="#17334f")
            draw.text((129, 169), "Microphone connected", fill="#52677a")
    else:
        draw.rectangle((0, 0, 479, 269), fill="#283242")
        for x in range(0, 480, 32):
            draw.line((x, 0, x, 270), fill="#303c4d")
        for y in range(0, 270, 32):
            draw.line((0, y, 480, y), fill="#303c4d")
        draw.rectangle((15, 14, 170, 54), fill="#111827", outline="#758195")
        draw.text((26, 27), description.upper(), fill="white")
        if state == "low_health":
            draw.rectangle((24, 72, 205, 92), fill="#461b25")
            draw.rectangle((27, 75, 73, 89), fill="#ef3340")
            draw.text((83, 76), "HEALTH  18%", fill="white")
            draw.rectangle((321, 25, 456, 238), outline="#aab7c6")
            draw.ellipse((365, 101, 415, 151), fill="#be8554")
        elif state == "inventory":
            draw.rectangle((111, 46, 369, 238), fill="#1a202a", outline="#b8c0cb", width=2)
            draw.text((130, 60), "INVENTORY", fill="white")
            for row in range(3):
                for column in range(4):
                    x, y = 132 + column * 51, 95 + row * 43
                    draw.rectangle((x, y, x + 39, y + 31), fill="#526176", outline="#9ba7b8")
        elif state == "death":
            draw.rectangle((0, 98, 479, 179), fill="#791f2b")
            draw.text((178, 126), "YOU DIED", fill="white")
            draw.text((181, 149), "Respawn   Main menu", fill="#f5d6d8")
        elif state == "camera_collision":
            draw.rectangle((250, 54, 480, 270), fill="#485463")
            draw.polygon(((215, 270), (328, 93), (480, 224), (480, 270)), fill="#394858")
            draw.ellipse((200, 118, 244, 162), fill="#d8a064")
            draw.text((24, 221), "CAMERA CLIPPED AT WALL", fill="#ffe178")
    image.info["benchmark_label"] = label
    return image


def build_cases() -> list[BenchmarkCase]:
    """Create compact, deterministic synthetic screenshots with known labels."""
    specifications = [
        ("website_nav_overlap", "website", "mobile navigation overlaps the page title", "#76a6d8", "nav_overlap", "ResponsiveMenu.tsx", "Keep the mobile navigation row below the page heading at narrow widths."),
        ("website_error_banner", "website", "red error banner reports a gateway connection failure", "#dc5860", "error_banner", "GatewayStatus.tsx", "Render the red gateway error banner when the health request fails."),
        ("website_gpu_graph", "website", "dashboard graph shows GPU utilization over time", "#3983ca", "gpu_graph", "GpuMetricsChart.tsx", "Plot GPU utilization samples in the system dashboard chart."),
        ("website_voice_panel", "website", "voice panel shows an active microphone session", "#3a9b72", "voice_panel", "RealtimeVoicePanel.tsx", "Show the microphone connected state while the voice session listens."),
        ("game_low_health", "game", "game HUD shows critical low health during combat", "#e53945", "low_health", "HealthHud.gd", "Flash the health bar and show its current percentage below the combat threshold."),
        ("game_inventory", "game", "game is paused on the inventory item grid", "#8392aa", "inventory", "InventoryGrid.gd", "Open the inventory overlay and display the item slots in a four-column grid."),
        ("game_death_screen", "game", "game over screen offers respawn and main menu", "#9e2637", "death", "DeathScreen.gd", "Display the death state with respawn and return-to-menu actions."),
        ("game_camera_collision", "game", "camera clips into a wall beside the player", "#6b7787", "camera_collision", "CameraRig.gd", "Clamp the third-person camera before it intersects level geometry."),
    ]
    result = []
    for label, profile, query, accent, state, filename, code_detail in specifications:
        image = _screen(label, profile, query, accent, state)
        code = f"# {filename}\n# {code_detail}\ndef update_visual_state():\n    return '{state}'\n"
        result.append(BenchmarkCase(label, profile, query, query, code, state, image))
    return result
