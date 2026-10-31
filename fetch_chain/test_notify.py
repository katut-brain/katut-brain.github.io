import unittest
from notify import message


def st(**chs):
    return {"channels": {ch: {"methods": [{"method": "m%d" % i, "ok": ok} for i, ok in enumerate(oks)]}
                         for ch, oks in chs.items()}}


class NotifyTest(unittest.TestCase):
    def test_healthy_says_nothing(self):
        self.assertEqual(message(st(x_post=[True, True], youtube=[None, True])), "")  # 使えない(None)は壊れたと数えない

    def test_one_broken_and_all_broken(self):
        self.assertEqual(message(st(x_post=[False, True, True])), "🟡 X：取り方が1つ壊れた（ほかの取り方で読めている）")
        self.assertEqual(message(st(instagram=[False, False])), "🔴 Instagram：全部の取り方が壊れていて、今は読めない")

    def test_replacement_events(self):
        ev = ("added_trial channel=x_post name=x_publish_oembed replaces=x_fxtwitter\n"
              "candidate_failed channel=threads name=t why=x\npromoted channel=youtube name=yt_noembed after=7")
        self.assertEqual(message(st(), ev).splitlines(),
                         ["🔧 X：代わりの取り方を見つけて、試しに使い始めた", "✅ YouTube：試していた代わりの取り方を、正式に使うことにした"])


if __name__ == "__main__":
    unittest.main()
