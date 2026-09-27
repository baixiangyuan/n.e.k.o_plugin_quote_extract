"""core.py 单元测试：解析、合并去重、抽取、删除与统计。"""

import json

from core import QuoteLibrary, parse_d1_rows, parse_quotes_payload, quote_key, raw_quote


class TestParsePayload:
    def test_json_string_array(self):
        data = json.dumps(["第一条", "第二条"]).encode("utf-8")
        quotes = parse_quotes_payload(data)
        assert [q["text"] for q in quotes] == ["第一条", "第二条"]
        assert quotes[0]["author"] == ""

    def test_json_object_array(self):
        data = json.dumps([
            {"text": "路虽远行则将至", "author": "荀子", "tag": "励志"},
            {"quote": "事虽难做则必成", "author": "荀子", "tags": ["励志", "学习"]},
        ]).encode("utf-8")
        quotes = parse_quotes_payload(data)
        assert quotes[0]["tags"] == ["励志"]
        assert quotes[1]["text"] == "事虽难做则必成"
        assert quotes[1]["tags"] == ["励志", "学习"]

    def test_json_wrapper_object(self):
        data = json.dumps({"quotes": [{"text": "a", "source": "书"}]}).encode("utf-8")
        quotes = parse_quotes_payload(data)
        assert len(quotes) == 1
        assert quotes[0]["source"] == "书"

    def test_json_string_nested_json(self):
        # 有些接口把 JSON 再套一层字符串
        inner = json.dumps([{"text": "嵌套"}])
        quotes = parse_quotes_payload(json.dumps(inner).encode("utf-8"))
        assert quotes[0]["text"] == "嵌套"

    def test_txt_lines(self):
        quotes = parse_quotes_payload("第一条\n第二条\n\n第三条".encode())
        assert [q["text"] for q in quotes] == ["第一条", "第二条", "第三条"]

    def test_txt_author_separator(self):
        quotes = parse_quotes_payload("路虽远行则将至 —— 荀子".encode())
        assert quotes[0]["text"] == "路虽远行则将至"
        assert quotes[0]["author"] == "荀子"

    def test_txt_author_paragraph(self):
        text = "愿你出走半生，归来仍是少年。\n佚名\n\n第二段语录"
        quotes = parse_quotes_payload(text.encode("utf-8"))
        assert quotes[0]["author"] == "佚名"
        assert quotes[1]["text"] == "第二段语录"

    def test_txt_comment_lines_skipped(self):
        quotes = parse_quotes_payload("# 这是注释\n真语录".encode())
        assert [q["text"] for q in quotes] == ["真语录"]

    def test_gbk_decoded(self):
        quotes = parse_quotes_payload("中文语录".encode("gbk"))
        assert quotes[0]["text"] == "中文语录"

    def test_bom_stripped(self):
        quotes = parse_quotes_payload(b"\xef\xbb\xbf" + json.dumps(["BOM"]).encode("utf-8"))
        assert quotes[0]["text"] == "BOM"

    def test_empty_payload(self):
        assert parse_quotes_payload(b"") == []
        assert parse_quotes_payload(b"   \n  ") == []

    def test_blank_entries_dropped(self):
        assert parse_quotes_payload(json.dumps(["", "  ", "ok"]).encode("utf-8"))[0]["text"] == "ok"


class TestMerge:
    def test_merge_dedupes(self):
        lib = QuoteLibrary()
        stats = lib.merge([raw_quote("A"), raw_quote(" A ")])
        assert stats["imported"] == 1
        assert stats["total"] == 1

    def test_merge_updates_fields(self):
        lib = QuoteLibrary()
        lib.merge([raw_quote("A", author="旧")])
        stats = lib.merge([raw_quote("A", author="新", tags=["tag1"])])
        assert stats["updated"] == 1
        assert stats["skipped"] == 0
        q = lib.by_id(1)
        assert q["author"] == "新"
        assert q["tags"] == ["tag1"]

    def test_merge_skips_identical(self):
        lib = QuoteLibrary()
        lib.merge([raw_quote("A", author="x")])
        stats = lib.merge([raw_quote("A", author="x")])
        assert stats["skipped"] == 1 and stats["imported"] == 0

    def test_ids_increment(self):
        lib = QuoteLibrary()
        lib.merge([raw_quote("1"), raw_quote("2"), raw_quote("3")])
        assert [lib.by_id(i)["id"] for i in (1, 2, 3)] == [1, 2, 3]

    def test_roundtrip(self):
        lib = QuoteLibrary()
        lib.merge([raw_quote("A", tags=["t"])])
        lib.draw(no_repeat_count=0)
        clone = QuoteLibrary(json.loads(json.dumps(lib.to_dict())))
        assert clone.stats()["total"] == 1
        assert clone.history == lib.history


class TestDraw:
    def _lib(self, n=5, tag="t"):
        lib = QuoteLibrary()
        lib.merge([raw_quote(f"语录{i}", tags=[tag] if i % 2 else []) for i in range(n)])
        return lib

    def test_empty_library_returns_none(self):
        assert QuoteLibrary().draw() is None

    def test_tag_filter(self):
        lib = self._lib()
        for _ in range(20):
            picked = lib.draw(tag="t", no_repeat_count=0)
            assert "t" in picked["tags"]

    def test_unknown_tag_returns_none(self):
        assert self._lib().draw(tag="不存在") is None

    def test_no_repeat(self):
        lib = self._lib(n=3)
        picked_keys = [lib.draw(no_repeat_count=2)["key"] for _ in range(2)]
        assert picked_keys[0] != picked_keys[1]

    def test_history_capped(self):
        lib = self._lib(n=2)
        for _ in range(300):
            lib.draw(no_repeat_count=0)
        assert len(lib.history) == 200


class TestRemoveAndStats:
    def test_remove_by_id_and_text(self):
        lib = QuoteLibrary()
        lib.merge([raw_quote("A"), raw_quote("B")])
        assert lib.remove(quote_id=1)["text"] == "A"
        assert lib.remove(text="B")["text"] == "B"
        assert lib.remove(quote_id=99) is None

    def test_stats(self):
        lib = QuoteLibrary()
        lib.merge([raw_quote("A", tags=["励志"]), raw_quote("B", tags=["励志", "学习"])])
        stats = lib.stats()
        assert stats["total"] == 2
        assert stats["tags"] == {"励志": 2, "学习": 1}


class TestD1Rows:
    def test_rows_to_quotes(self):
        rows = [
            {"text": "A", "author": "甲", "source": "书", "tag": "励志"},
            {"quote": "B", "tags": "a,b"},
            {"text": None},
        ]
        quotes = parse_d1_rows(rows)
        assert len(quotes) == 2
        assert quotes[0]["author"] == "甲"
        assert quotes[1]["tags"] == ["a", "b"]


def test_quote_key_stable():
    assert quote_key("一  二") == quote_key("一 二")
    assert quote_key("一") != quote_key("二")
