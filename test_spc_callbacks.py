"""Kiểm tra nút SPC ngoại tuyến, không khởi động bot hoặc truy cập mạng."""
import ast
import datetime
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def check_spc_callbacks():
    source = Path(__file__).with_name("bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        "spc_commune_key", "resolve_spc_commune", "handle_spc_bureau",
        "handle_spc_commune_select", "handle_spc_ref_reload",
    }
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(functions) == len(names)
    for node in functions:
        node.decorator_list = []

    def button(text, callback_data):
        assert 1 <= len(callback_data.encode("utf-8")) <= 64, callback_data
        return SimpleNamespace(text=text, callback_data=callback_data)

    bot = Mock()
    format_messages = Mock(return_value=["Lịch thử"])
    namespace = {
        "hashlib": hashlib, "datetime": datetime, "bot": bot,
        "InlineKeyboardButton": button, "InlineKeyboardMarkup": Mock,
        "safe_answer_callback": Mock(), "safe_delete_message": Mock(),
        "USER_SESSIONS": {}, "extract_commune": lambda location: location,
        "format_outage_messages": format_messages,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), "bot.py", "exec"), namespace)
    commune = "Phường Nguyễn Thị Minh Khai_Khu vực mở rộng " * 4
    outages = [{"location": commune}, {"location": "Xã khác"}]
    namespace["fetch_raw_outages_spc"] = Mock(return_value=outages)
    call = SimpleNamespace(id="test", data="spc_bur_PB0101", message=SimpleNamespace(
        chat=SimpleNamespace(id=1), message_id=2))
    namespace["handle_spc_bureau"](call)
    markup = bot.edit_message_text.call_args.kwargs["reply_markup"]
    selected = next(item for item in markup.add.call_args.args if item.text.startswith(commune))
    call.data = selected.callback_data
    namespace["handle_spc_commune_select"](call)
    assert format_messages.call_args.args[0] == [outages[0]]
    reload_button = bot.send_message.call_args.kwargs["reply_markup"].row.call_args.args[1]
    namespace["USER_SESSIONS"].clear()
    namespace["fetch_raw_outages_spc"].return_value = list(reversed(outages))
    call.data = reload_button.callback_data
    namespace["handle_spc_ref_reload"](call)
    assert format_messages.call_args.args[0] == [outages[0]]
    call.data = "spc_ref_reload_PB0101_" + commune
    namespace["handle_spc_ref_reload"](call)
    assert format_messages.call_args.args[0] == [outages[0]]
    call.data = "spc_comm_all"
    namespace["handle_spc_commune_select"](call)
    assert format_messages.call_args.args[0] == list(reversed(outages))
    format_messages.reset_mock()
    call.data = "spc_comm_invalid"
    namespace["handle_spc_commune_select"](call)
    format_messages.assert_not_called()
    call.data = "spc_ref_reload_"
    namespace["fetch_raw_outages_spc"].reset_mock()
    namespace["handle_spc_ref_reload"](call)
    namespace["fetch_raw_outages_spc"].assert_not_called()
    namespace["fetch_raw_outages_spc"].return_value = [{"location": "Xã khác"}]
    call.data = reload_button.callback_data
    namespace["handle_spc_ref_reload"](call)
    format_messages.assert_not_called()
    print("OK: nút SPC, tải lại, nút cũ, tất cả, dữ liệu lỗi, khu vực biến mất")


if __name__ == "__main__":
    check_spc_callbacks()

