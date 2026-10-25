import unittest
from unittest.mock import patch
import subprocess

from bluetooth_manager import BluetoothManager
from wifi_manager import WifiManager


class ParserTests(unittest.TestCase):
    def test_nmcli_scan_parser_handles_escaped_ssid_and_duplicates(self):
        output = "Home\\:5G:55:WPA2\nHome\\:5G:88:WPA2\nGuest:30:--\n"
        networks = WifiManager.parse_scan_output(output)
        self.assertEqual([item.ssid for item in networks], ["Home:5G", "Guest"])
        self.assertEqual(networks[0].signal, 88)
        self.assertTrue(networks[1].is_open)

    def test_bluetooth_device_parser(self):
        output = "Device AA:BB:CC:DD:EE:FF M10 Headset\nDevice 11:22:33:44:55:66 Keyboard\n"
        devices = BluetoothManager.parse_devices(output)
        self.assertEqual(devices[0].address, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(devices[0].name, "M10 Headset")
        self.assertEqual(len(devices), 2)

    def test_wifi_status_extracts_indexed_ipv4_field(self):
        manager = WifiManager()
        output = (
            "GENERAL.STATE:100 (connected)\n"
            "GENERAL.CONNECTION:Home\n"
            "IP4.ADDRESS[1]:192.168.1.42/24\n"
        )
        result = subprocess.CompletedProcess([], 0, output)
        with patch("wifi_manager.shutil.which", return_value="nmcli"), patch.object(manager, "_run", return_value=result):
            status = manager.status()
        self.assertTrue(status.connected)
        self.assertEqual(status.ssid, "Home")
        self.assertEqual(status.ip_address, "192.168.1.42")

    def test_wifi_status_does_not_mistake_disconnected_for_connected(self):
        # "disconnected" contains the substring "connected"; getting this wrong
        # reports a live link with no network and enables the radio buttons.
        manager = WifiManager()
        output = (
            "GENERAL.STATE:30 (disconnected)\n"
            "GENERAL.CONNECTION:--\n"
            "IP4.ADDRESS[1]:\n"
        )
        result = subprocess.CompletedProcess([], 0, output)
        with patch("wifi_manager.shutil.which", return_value="nmcli"), patch.object(manager, "_run", return_value=result):
            status = manager.status()
        self.assertFalse(status.connected)
        self.assertEqual(status.ssid, "")
        self.assertEqual(status.state, "未连接")

    def test_bluetooth_address_alias_is_not_a_name(self):
        # BlueZ aliases a nameless BLE beacon to its own address, which used to
        # show up on screen as e.g. "7D-08-D2-FF-F0-1R".
        output = (
            "Device 6E:B8:20:6E:39:5D 6E-B8-20-6E-39-5D\n"
            "Device 64:68:76:EE:BB:53 EDIFIER BLE\n"
        )
        devices = BluetoothManager.parse_devices(output)
        self.assertFalse(devices[0].has_name)
        self.assertTrue(devices[1].has_name)
        self.assertEqual(devices[1].name, "EDIFIER BLE")

    def test_bluetooth_info_rejects_separator_style_address_alias(self):
        manager = BluetoothManager()
        output = (
            "Device 6E:B8:20:6E:39:5D (public)\n"
            "\tAlias: 6E-B8-20-6E-39-5D\n"
            "\tPaired: no\n"
            "\tConnected: no\n"
        )
        result = subprocess.CompletedProcess([], 0, output)
        with patch.object(manager, "_run", return_value=result):
            device = manager.info("6E:B8:20:6E:39:5D")
        self.assertFalse(device.has_name)
        self.assertEqual(device.address, "6E:B8:20:6E:39:5D")

    def test_bluetooth_properties_tolerate_tab_indentation(self):
        # Real bluetoothctl output indents each property with a tab.  The old
        # pattern anchored on the bare name, matched nothing, and so reported
        # every device as unpaired and disconnected -- the UI then claimed a
        # failed connection while audio was already playing.
        output = (
            "Device 64:68:76:B1:52:3A (public)\n"
            "\tName: EDIFIER X1\n"
            "\tAlias: EDIFIER X1\n"
            "\tClass: 0x00240404\n"
            "\tPaired: yes\n"
            "\tTrusted: yes\n"
            "\tConnected: yes\n"
        )
        result = subprocess.CompletedProcess([], 0, output)
        manager = BluetoothManager()
        with patch.object(manager, "_run", return_value=result):
            device = manager.info("64:68:76:B1:52:3A")
        self.assertTrue(device.paired, "Paired: yes was not detected")
        self.assertTrue(device.trusted, "Trusted: yes was not detected")
        self.assertTrue(device.connected, "Connected: yes was not detected")
        self.assertEqual(device.name, "EDIFIER X1")

    def test_pair_output_classification(self):
        self.assertTrue(BluetoothManager._pair_succeeded("Pairing successful"))
        # Already-paired devices must not be reported as a hard failure.
        self.assertTrue(BluetoothManager._pair_succeeded("Failed to pair: org.bluez.Error.AlreadyExists"))
        self.assertFalse(BluetoothManager._pair_succeeded("Failed to pair: org.bluez.Error.AuthenticationTimeout"))

    def test_error_text_strips_ansi_and_prompt_noise(self):
        raw = "\x1b[0;94m[bluetooth]\x1b[0m# pair AA:BB\n\tFailed to pair: org.bluez.Error.AuthenticationTimeout\n"
        message = BluetoothManager._friendly_error(raw, "蓝牙配对失败")
        self.assertNotIn("\x1b", message)
        self.assertIn("配对超时", message)

    def test_wifi_status_reports_ssid_not_profile_name(self):
        # GENERAL.CONNECTION is the NetworkManager *profile* name.  The stock
        # M10 image names that profile literally "wifi", so the screen read
        # "SSID: wifi" whatever network was joined.  The real SSID lives in the
        # profile's 802-11-wireless.ssid property.
        manager = WifiManager()
        device_show = subprocess.CompletedProcess([], 0, (
            "GENERAL.STATE:100 (connected)\n"
            "GENERAL.CONNECTION:wifi\n"
            "IP4.ADDRESS[1]:192.168.2.103/24\n"
        ))
        profile_show = subprocess.CompletedProcess([], 0, "802-11-wireless.ssid:TP-LINK_D585\n")
        with patch("wifi_manager.shutil.which", return_value="nmcli"), \
                patch.object(manager, "_run", side_effect=[device_show, profile_show]):
            status = manager.status()
        self.assertTrue(status.connected)
        self.assertEqual(status.ssid, "TP-LINK_D585")
        self.assertEqual(status.ip_address, "192.168.2.103")

    def test_wifi_status_unescapes_colon_in_ssid(self):
        manager = WifiManager()
        device_show = subprocess.CompletedProcess([], 0, (
            "GENERAL.STATE:100 (connected)\n"
            "GENERAL.CONNECTION:Home\\:5G\n"
        ))
        profile_show = subprocess.CompletedProcess([], 0, "802-11-wireless.ssid:Home\\:5G\n")
        with patch("wifi_manager.shutil.which", return_value="nmcli"), \
                patch.object(manager, "_run", side_effect=[device_show, profile_show]):
            status = manager.status()
        self.assertEqual(status.ssid, "Home:5G")

    def test_wifi_status_falls_back_when_profile_lookup_fails(self):
        manager = WifiManager()
        device_show = subprocess.CompletedProcess([], 0, (
            "GENERAL.STATE:100 (connected)\n"
            "GENERAL.CONNECTION:wifi\n"
        ))
        profile_show = subprocess.CompletedProcess([], 10, "")
        with patch("wifi_manager.shutil.which", return_value="nmcli"), \
                patch.object(manager, "_run", side_effect=[device_show, profile_show]):
            status = manager.status()
        self.assertEqual(status.ssid, "wifi")

    def test_clip_keeps_labels_on_one_line(self):
        try:
            from main import RadioApp
        except ImportError:
            self.skipTest("unihiker GUI is only installed on the M10")

        self.assertEqual(RadioApp._clip("Short", 10), "Short")
        # Each CJK glyph costs a full unit, Latin about half.
        self.assertEqual(RadioApp._clip("中文字符测试一二三", 4), "中文字符")
        self.assertEqual(RadioApp._clip("ABCDEFGHIJ", 3), "ABCDE")


if __name__ == "__main__":
    unittest.main()
