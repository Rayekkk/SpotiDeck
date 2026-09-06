import io
import unittest
from unittest.mock import Mock, patch

from backend.spotify import API, TOKEN_URL, SpotifyError, http


class Response(io.BytesIO):
    def __init__(self, body, status=200):
        super().__init__(body)
        self.status = status
        self.headers = {}


class HTTPCommandTests(unittest.TestCase):
    def call(self, method, url, body, status=200):
        opener = Mock()
        opener.open.return_value = Response(body, status)
        with patch("backend.spotify.https_opener", return_value=opener):
            return http(method, url)

    def test_successful_transport_accepts_non_json_acknowledgements(self):
        for path, method in [("pause", "PUT"), ("play", "PUT"), ("next", "POST")]:
            with self.subTest(path=path):
                status, _, data = self.call(method, API + "/me/player/" + path, b"Command accepted")
                self.assertEqual(status, 200)
                self.assertIsNone(data)

    def test_no_content_success_is_accepted(self):
        self.assertEqual(self.call("PUT", API + "/me/player/pause", b"", 204), (204, {}, None))

    def test_oauth_still_parses_tokens_and_rejects_invalid_data(self):
        self.assertEqual(self.call("POST", TOKEN_URL, b'{"access_token":"test"}')[2], {"access_token": "test"})
        with self.assertRaises(SpotifyError):
            self.call("POST", TOKEN_URL, b"not valid token data")

    def test_playback_reads_cannot_treat_invalid_json_as_valid_state(self):
        with self.assertRaises(SpotifyError) as raised:
            self.call("GET", API + "/me/player", b"private response contents")
        self.assertNotIn("private", str(raised.exception))
        self.assertIsNone(self.call("GET", API + "/me/player", b" \n ")[2])

    def test_playlist_creation_and_edits_retain_json_identifiers(self):
        self.assertEqual(self.call("POST", API + "/me/playlists", b'{"id":"created-playlist"}', 201)[2],
                         {"id": "created-playlist"})
        for method in ("POST", "PUT", "DELETE"):
            with self.subTest(method=method):
                path = API + "/playlists/" + "a" * 22 + "/items"
                self.assertEqual(self.call(method, path, b'{"snapshot_id":"revision"}')[2],
                                 {"snapshot_id": "revision"})
                with self.assertRaises(SpotifyError):
                    self.call(method, path, b"unexpected response")

    def test_library_write_keeps_compatible_plaintext_ack(self):
        self.assertIsNone(self.call("PUT", API + "/me/library?uris=test", b"Command accepted")[2])


if __name__ == "__main__":
    unittest.main()
