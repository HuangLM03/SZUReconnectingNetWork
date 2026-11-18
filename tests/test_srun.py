import unittest
from unittest.mock import Mock

from szu_guardian.srun import SrunClient, hmac_md5, srun_base64, xencode


def response(text, url="https://net.szu.edu.cn/"):
    value = Mock()
    value.status_code = 200
    value.text = text
    value.url = url
    value.headers = {}
    value.raise_for_status.return_value = None
    return value


class SrunCryptoTests(unittest.TestCase):
    def test_upstream_vectors_unchanged(self):
        self.assertEqual(srun_base64(b"E99p1ant"), "hFYeMJiTWq+=")
        self.assertEqual(
            xencode("aaaaaaaaaaaa", "bbbbbbbbbbbb"),
            bytes.fromhex("d4eb3234a6e57d455fdca5bcbefb3ad1"),
        )
        self.assertEqual(
            hmac_md5(
                "123",
                "a765ff3138693dbfa6888f05726588de53db72e1679a9341f04f9195d2ee3b8e",
            ),
            "b4cc9d0fcff069fcd31afae0b7001434",
        )


class SrunClientTests(unittest.TestCase):
    def test_online_status_requires_consistent_fields_and_matching_user(self):
        session = Mock()
        session.headers = {}
        session.get.return_value = response(
            '_({"error":"ok","res":"ok","user_name":"user"})'
        )
        client = SrunClient("user", "password", session=session)
        self.assertTrue(client.is_online())
        self.assertTrue(session.get.call_args.kwargs["verify"])

    def test_contradictory_online_fields_are_unknown(self):
        for text in (
            '_({"error":"ok","res":"not_online_error"})',
            '_({"error":"not_online_error","res":"ok"})',
        ):
            session = Mock()
            session.headers = {}
            session.get.return_value = response(text)
            self.assertIsNone(SrunClient("user", "password", session=session).is_online())

    def test_other_users_session_is_not_current_session(self):
        session = Mock()
        session.headers = {}
        session.get.return_value = response(
            '_({"error":"ok","res":"ok","user_name":"someone-else"})'
        )
        self.assertIsNone(SrunClient("user", "password", session=session).is_online())

    def test_unexpected_jsonp_callback_is_rejected(self):
        session = Mock()
        session.headers = {}
        session.get.return_value = response('attacker({"error":"ok","res":"ok"})')
        with self.assertRaises(ConnectionError):
            SrunClient("user", "password", session=session).is_online()

    def test_missing_ac_id_fails_instead_of_silently_using_default(self):
        session = Mock()
        session.headers = {}
        session.get.side_effect = [response("no ac_id"), response("still missing")]
        with self.assertRaises(ConnectionError):
            SrunClient("user", "password", session=session).discover_ac_id()

    def test_ac_id_can_be_read_from_same_origin_index_page(self):
        session = Mock()
        session.headers = {}
        session.get.side_effect = [
            response("no ac_id"),
            response('<a href="/portal?ac_id=9">login</a>'),
        ]
        ac_id = SrunClient("user", "password", session=session).discover_ac_id()
        self.assertEqual(ac_id, "9")

    def test_login_requires_consistent_success_response(self):
        session = Mock()
        session.headers = {}
        session.get.side_effect = [
            response('<meta content="0;url=/srun_portal_pc?ac_id=1">'),
            response('_({"challenge":"abc123","client_ip":"172.30.1.2","error":"ok","res":"ok"})'),
            response('_({"error":"ok","res":"ok","st":1})'),
        ]

        result = SrunClient("user", "password", session=session).login()

        self.assertTrue(result.success)
        self.assertEqual(result.client_ip, "172.30.1.2")
        self.assertEqual(session.get.call_args_list[-1].kwargs["params"]["ac_id"], "1")

    def test_login_rejects_contradictory_success_fields(self):
        for portal_text in (
            '_({"error":"ok","res":"failure","st":1})',
            '_({"error":"failure","res":"ok","st":1})',
            '_({"error":"failure","res":"failure","st":1})',
        ):
            session = Mock()
            session.headers = {}
            session.get.side_effect = [
                response('<meta content="0;url=/srun_portal_pc?ac_id=1">'),
                response('_({"challenge":"abc123","client_ip":"172.30.1.2","error":"ok","res":"ok"})'),
                response(portal_text),
            ]
            result = SrunClient("user", "password", session=session).login()
            self.assertFalse(result.success)
            self.assertTrue(result.rejected)

    def test_challenge_ip_must_match_direct_adapter(self):
        session = Mock()
        session.headers = {}
        session.get.side_effect = [
            response('<meta content="0;url=/srun_portal_pc?ac_id=1">'),
            response('_({"challenge":"abc123","client_ip":"172.30.9.9","error":"ok","res":"ok"})'),
        ]

        result = SrunClient(
            "user",
            "password",
            session=session,
            expected_client_ip="172.30.1.2",
        ).login()

        self.assertFalse(result.success)
        self.assertIn("不一致", result.message)
        self.assertEqual(session.get.call_count, 2)


if __name__ == "__main__":
    unittest.main()
