from __future__ import annotations

import unittest

from seebx.contracts.search import (
    TEXT_SEARCH_AUTHORIZATION_BASIS,
    VOICE_SEARCH_AUTHORIZATION_BASIS,
    SearchCapabilityManifestV1,
)
from seebx.capabilities.search.output_validation import (
    SearchCapabilityOutputValidationError,
    validate_search_capability_output_v1,
)


class SearchCapabilityOutputValidatorV1Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = SearchCapabilityManifestV1.create(
            authorization_basis=TEXT_SEARCH_AUTHORIZATION_BASIS,
        )

    def test_accepts_exact_bounded_voice_answer(self) -> None:
        answer = (
            "When needed, I can use server-mediated research routes for "
            "current news and recent events, trusted health and medical "
            "information, and software and cybersecurity current events. "
            "The server controls which route, sources, and research budget "
            "are available. This is not unrestricted browsing, and access to "
            "a particular website or page is not guaranteed. No internet "
            "research was run for this response."
        )
        voice_manifest = SearchCapabilityManifestV1.create(
            authorization_basis=VOICE_SEARCH_AUTHORIZATION_BASIS,
        )
        validate_search_capability_output_v1(answer, voice_manifest)

    def test_accepts_corrected_bounded_text_answer(self) -> None:
        answer = (
            "I can use server-mediated internet research for current news, "
            "trusted health information, and software or cybersecurity "
            "current events. The server selects the route and permitted "
            "sources. Research was not performed for this response."
        )
        validate_search_capability_output_v1(answer, self.manifest)

    def test_accepts_scoped_negative_capability_wording(self) -> None:
        answers = (
            (
                "I can't use internet research for unrestricted or "
                "general-purpose browsing, arbitrary webpage retrieval, or "
                "broad fact-checking outside the supported categories."
            ),
            "I cannot search the web for arbitrary pages.",
            (
                "I can't use the internet for:\n"
                "- unrestricted browsing\n"
                "- arbitrary page retrieval"
            ),
            (
                "I cannot use the internet outside the supported categories "
                "or routes."
            ),
        )
        for answer in answers:
            with self.subTest(answer=answer):
                validate_search_capability_output_v1(
                    answer,
                    self.manifest,
                )

    def test_rejects_reported_overbroad_text_answer(self) -> None:
        with self.assertRaisesRegex(
            SearchCapabilityOutputValidationError,
            "exceeds_authorized_scope",
        ):
            validate_search_capability_output_v1(
                "Other supported fact-checking or source-verification tasks.",
                self.manifest,
            )

    def test_rejects_global_access_denials(self) -> None:
        denied = (
            "I cannot access sources.",
            "I don't have access to the internet.",
            "I can't check the web from this chat.",
            "I don't have the ability to browse the internet.",
            "I'm unable to search current news.",
            "This chat cannot search the web.",
            "The system has no web access.",
        )
        for answer in denied:
            with self.subTest(answer=answer):
                with self.assertRaisesRegex(
                    SearchCapabilityOutputValidationError,
                    "denies_authorized_access",
                ):
                    validate_search_capability_output_v1(
                        answer,
                        self.manifest,
                    )

    def test_rejects_unbounded_capability_claims(self) -> None:
        claims = (
            "I can search the web for anything.",
            "This system may access any website.",
            "The service provides unrestricted web browsing.",
        )
        for answer in claims:
            with self.subTest(answer=answer):
                with self.assertRaisesRegex(
                    SearchCapabilityOutputValidationError,
                    "exceeds_authorized_scope",
                ):
                    validate_search_capability_output_v1(
                        answer,
                        self.manifest,
                    )

    def test_without_manifest_does_not_change_ordinary_answers(self) -> None:
        validate_search_capability_output_v1(
            "I cannot access sources.",
            None,
        )


if __name__ == "__main__":
    unittest.main()
