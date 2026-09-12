from tc_template.fblib import find_fbs, list_fbs, render_fb, validate_fb


COMMUNICATION_SLUGS = {
    "tcpip-client",
    "tcpip-server",
    "tcpip-send",
    "tcpip-receive",
    "ads-read-symbol",
    "ads-write-symbol",
}


def test_common_communication_templates_are_listed():
    slugs = {item["slug"] for item in list_fbs("communication")}
    assert COMMUNICATION_SLUGS <= slugs


def test_communication_templates_render_without_placeholders():
    for slug in COMMUNICATION_SLUGS:
        rendered = render_fb(slug)
        assert "{{" not in rendered["declaration"]
        assert "{{" not in rendered["implementation"]


def test_new_communication_templates_pass_offline_lint():
    for slug in COMMUNICATION_SLUGS - {"tcpip-client"}:
        result = validate_fb(slug)
        assert not result["findings"], (slug, result["findings"])


def test_find_routes_tcp_server_and_ads_read():
    assert find_fbs("做一个 TCP 服务端监听")["results"][0]["slug"] == "tcpip-server"
    assert find_fbs("通过 ADS 按符号名读变量")["results"][0]["slug"] == "ads-read-symbol"


def test_tcp_client_is_oop_with_send_receive_methods():
    rendered = render_fb("tcpip-client")
    client = next(item for item in rendered["objects"] if item["name"] == "FB_TcpIpClient")
    assert {member["name"] for member in client["members"]} == {"Cyclic", "Send", "Receive"}
