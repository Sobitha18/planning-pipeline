"""Files named in a request: stack frames and mentioned paths, matched against
each repo's index. Pure functions, no DB."""

from src import report_files as rf

PATHS = {
    "api": {"app/routers/orders.py", "app/models.py", "app/utils/helpers.py", "src/index.ts"},
    "web": {"src/screens/Checkout.tsx", "src/api/orders.ts", "src/index.ts", "src/lib/utils/guideline.ts"},
}


def found(text_list):
    return [(f.repo, f.path, f.line) for f in rf.locate(rf.extract_mentions(text_list), PATHS)]


def test_python_traceback_frames_are_found_with_their_line_numbers():
    trace = '''Traceback (most recent call last):
  File "/srv/harbor/app/routers/orders.py", line 42, in place_order
    raise HTTPException(409)
  File "/usr/lib/python3.12/site-packages/fastapi/routing.py", line 10, in handler
'''
    assert found([trace]) == [("api", "app/routers/orders.py", 42)]      # the srv prefix is ignored, the library frame isn't in the index


def test_js_stack_frames_including_webpack_prefixes():
    trace = "TypeError: x is undefined\n    at onPay (webpack-internal:///./src/screens/Checkout.tsx:18:7)\n    at node_modules/react/index.js:5:1"
    assert found([trace]) == [("web", "src/screens/Checkout.tsx", 18)]


def test_paths_mentioned_in_plain_text_with_and_without_line_numbers():
    text = "Total is wrong. See app/models.py:77 and also src/api/orders.ts, maybe `app/utils/helpers.py`."
    assert found([text]) == [
        ("api", "app/models.py", 77),
        ("web", "src/api/orders.ts", None),
        ("api", "app/utils/helpers.py", None),
    ]


def test_a_shortened_path_matches_the_longer_indexed_one():
    assert found(["the bug is in lib/utils/guideline.ts"]) == [("web", "src/lib/utils/guideline.ts", None)]


def test_a_bare_file_name_matches_in_every_repo_that_has_it_and_only_there():
    assert found(["check orders.py"]) == [("api", "app/routers/orders.py", None)]
    assert found(["look at index.ts"]) == [("api", "src/index.ts", None), ("web", "src/index.ts", None)]


def test_a_mentioned_file_that_is_not_indexed_is_never_returned():
    assert found(["File \"/x/y/ghost.py\", line 3, in f\nsee also missing/other.ts"]) == []


def test_no_paths_means_nothing_located():
    assert found(["Customers can't see their order totals on the checkout page."]) == []


def test_duplicates_collapse_and_vague_mentions_are_capped():
    paths = {"r": {f"pkg{i}/index.ts" for i in range(10)}}
    got = rf.locate(rf.extract_mentions(["index.ts", "index.ts"]), paths)
    assert len(got) == rf.MAX_MATCHES_PER_MENTION_PER_REPO
