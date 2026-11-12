"""Print the address to open GoldPulse AI on a phone connected to the same Wi-Fi."""
import socket


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))      # no data is sent; this only picks the Wi-Fi interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


if __name__ == "__main__":
    ip = local_ip()
    print("=" * 60)
    print("  OPEN THIS ON YOUR PHONE (same Wi-Fi as this PC):")
    print(f"      http://{ip}:8010")
    print("=" * 60)
    if ip.startswith("127."):
        print("  Could not find the Wi-Fi address – is this PC connected to Wi-Fi?")
    print("  If Windows asks about Python and networks: allow PRIVATE networks only.")
    print("  Use only on your home Wi-Fi, never on public Wi-Fi.\n")
