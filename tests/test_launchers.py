import pytest
from avd_feed_connect.gui import launchers


def test_connect_uri_round_trip_and_rejects_other_input():
    uri = launchers.connect_uri('c922', '55ff5ad2-5b63')
    assert launchers.parse_connect_uri(uri) == ('c922', '55ff5ad2-5b63')
    for bad in ('cloudpcconnect://connect/a', 'cloudpcconnect://other/a/b', 'https://connect/a/b',
                'cloudpcconnect://connect/a/..%2Fb', 'cloudpcconnect://connect/a/b%20c', None):
        assert launchers.parse_connect_uri(bad) is None


def test_desktop_entry_is_single_line_per_key():
    entry = launchers.desktop_entry('app.id', 'juno\nExec=evil', 'acct', 'res')
    assert 'Exec=cloud-pc-connect cloudpcconnect://connect/acct/res' in entry.splitlines()
    assert not any(line.startswith('Exec=evil') for line in entry.splitlines())
    assert launchers.desktop_file_id('app.id', 'acct', 'res').startswith('app.id.pc_')


def test_icon_is_a_square_png_with_badge(tmp_path):
    cairo = pytest.importorskip('cairo')
    data = launchers.render_icon('teams-devbox', palette={'accent': '#ff0000'})
    path = tmp_path / 'icon.png'
    path.write_bytes(data)
    surface = cairo.ImageSurface.create_from_png(str(path))
    assert (surface.get_width(), surface.get_height()) == (launchers.ICON_SIZE, launchers.ICON_SIZE)
    stride, pixels = surface.get_stride(), surface.get_data()
    x = y = int(launchers.ICON_SIZE * 0.66)   # inside the badge, off the glyph
    b, g, r, a = pixels[y * stride + x * 4: y * stride + x * 4 + 4]
    assert (r, g, b, a) == (255, 0, 0, 255)


def test_ico_to_png_picks_the_largest_png_entry():
    import struct
    small, large = b'\x89PNG\r\n\x1a\nsmall', b'\x89PNG\r\n\x1a\nlarge-image'
    header = struct.pack('<HHH', 0, 1, 2)
    first = 6 + 16 * 2
    entries = struct.pack('<BBBBHHII', 32, 32, 0, 0, 1, 32, len(small), first)
    entries += struct.pack('<BBBBHHII', 0, 0, 0, 0, 1, 32, len(large), first + len(small))
    assert launchers.ico_to_png(header + entries + small + large) == large
    assert launchers.ico_to_png(b'not an icon') is None
