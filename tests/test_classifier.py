from illegal_site_bot.classifier import CONTACT_CATEGORY


class TestExclusions:
    def test_search_engine_is_excluded(self, classifier):
        verdict = classifier.classify("https://www.google.com/search?q=x", "구글 검색")
        assert verdict.excluded and verdict.score == 0

    def test_subdomain_of_excluded_domain_is_excluded(self, classifier):
        assert classifier.classify("https://cdn.naver.com/x").excluded

    def test_government_suffix_is_excluded(self, classifier):
        assert classifier.classify("https://www.police.go.kr/").excluded

    def test_static_asset_url_is_excluded(self, classifier):
        assert classifier.classify("https://a-site.xyz/img/banner.png").excluded

    def test_normal_site_is_not_excluded(self, classifier):
        assert not classifier.classify("https://casino-abc777.xyz/").excluded


class TestScoring:
    def test_gambling_keyword_in_anchor_scores_high(self, classifier):
        verdict = classifier.classify(
            "https://casino-abc777.xyz/", "에볼루션 라이브카지노 바카라"
        )
        assert verdict.category == "gambling"
        assert verdict.score >= classifier.rules.high_threshold

    def test_context_keyword_counts_less_than_anchor(self, classifier):
        anchor = classifier.classify("https://x-site.com/", "카지노 바카라 슬롯")
        context = classifier.classify("https://x-site.com/", "", "카지노 바카라 슬롯")
        assert anchor.score > context.score

    def test_adult_keyword_picks_adult_category(self, classifier):
        verdict = classifier.classify("https://freeya-dong.com/", "무료야동 새 주소")
        assert verdict.category == "adult"

    def test_domain_heuristic_alone_can_pass_threshold(self, classifier):
        # 텍스트가 전혀 없어도 도메인 형태 + 의심 TLD 로 후보가 됩니다.
        verdict = classifier.classify("https://baccarat-vip-365.club/")
        assert verdict.score >= classifier.rules.candidate_threshold
        assert not verdict.excluded

    def test_plain_unknown_site_stays_below_threshold(self, classifier):
        verdict = classifier.classify("https://plain-ad-frame.example/frame")
        assert verdict.score < classifier.rules.candidate_threshold

    def test_score_never_exceeds_100(self, classifier):
        text = "카지노 바카라 슬롯 토토 사설토토 먹튀검증 안전놀이터 꽁머니 첫충 홀덤 파워볼"
        assert classifier.classify("https://casino-bet-777.bet/", text).score <= 100

    def test_matched_keywords_are_reported(self, classifier):
        verdict = classifier.classify("https://some-toto-site.xyz/y", "안전놀이터 토토")
        assert "안전놀이터" in verdict.matched_keywords_text


class TestContactChannels:
    def test_telegram_is_contact_category(self, classifier):
        verdict = classifier.classify("https://t.me/promo_admin", "텔레그램 문의")
        assert verdict.category == CONTACT_CATEGORY
        assert verdict.always_keep is True

    def test_kakao_openchat_is_contact_category(self, classifier):
        assert classifier.classify("https://open.kakao.com/o/abc").category == CONTACT_CATEGORY


class TestAggregation:
    def test_cross_source_bonus_grows_then_caps(self, classifier):
        rules = classifier.rules
        assert classifier.final_score(40, 1) == 40
        assert classifier.final_score(40, 2) == 40 + rules.cross_source_bonus_per_source
        assert classifier.final_score(40, 99) == min(100, 40 + rules.cross_source_bonus_max)

    def test_risk_labels_follow_thresholds(self, classifier):
        rules = classifier.rules
        assert classifier.risk_label(rules.high_threshold) == "높음"
        assert classifier.risk_label(rules.candidate_threshold) == "보통"
        assert classifier.risk_label(rules.candidate_threshold - 1) == "낮음"
