import unittest
import job_monitor as jm

# Starting term lists mirror the new companies.yaml (Task 3). Kept inline so the
# filter behavior is tested independently of the config file.
TITLE_TERMS = [
    "site reliability", "SRE", "production engineer", "platform engineer",
    "cloud engineer", "cloud architect", "cloud infrastructure",
    "infrastructure engineer", "devops engineer", "devops", "devsecops",
    "ml platform", "ai platform", "solutions architect",
    "director of infrastructure", "director of platform", "head of platform",
    "engineering manager",
]
EXCLUDE_TERMS = [
    "intern", "junior", "jr", "new grad", "sales", "sales engineer",
    "recruiter", "compensation", "logistics", "supply chain", "real estate",
    "investment", "human resources", "HR business", "program manager",
    "product manager", "PR",
    # hardware / manufacturing / defense-physical (cut hardware-company noise)
    "manufacturing", "propulsion", "missile", "missiles", "rocket", "embedded",
    "flight software", "mechanical", "hardware", "avionics", "structural",
]
# remote terms + within-3h cities + united states + specific US cities under test
LOCATION_TERMS = jm.REMOTE_TERMS + [
    "portland", "beaverton", "hillsboro", "gresham", "salem", "eugene",
    "tigard", "bend", "corvallis", "seattle", "olympia", "tacoma",
    "albany", "medford", "hood river", "springfield",
    # US cities that share names with foreign places; included so the foreign-guard
    # tests can verify these are NOT dropped by the foreign guard.
    "dublin, ca", "vancouver, wa", "new mexico",
]


def mk(title, location):
    return {"title": title, "location": location}


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.tp = jm.compile_patterns(TITLE_TERMS)
        self.lp = jm.compile_patterns(LOCATION_TERMS, boundary=True)
        self.ep = jm.compile_patterns(EXCLUDE_TERMS, boundary=True)

    def keep(self, title, loc):
        return jm.matches(mk(title, loc), self.tp, self.lp, self.ep)

    # --- should KEEP ---
    def test_remote_sre_kept(self):
        self.assertTrue(self.keep("Senior Site Reliability Engineer", "United States"))

    def test_in_radius_metro_platform_kept(self):
        self.assertTrue(self.keep("Platform Engineer", "Hillsboro, OR"))

    def test_scoped_leadership_kept(self):
        self.assertTrue(self.keep("Director of Infrastructure", "Remote"))

    def test_wholesale_does_not_trigger_sales_exclude(self):
        # 'sales' must not match inside 'wholesale' (word boundary)
        self.assertTrue(self.keep("Wholesale Platform Engineer", "Remote"))

    # --- should DROP ---
    def test_sunnyvale_substring_regression(self):
        # Regression guard: short location tokens (e.g. "us") must not match as
        # substrings of words like "Houston". Sunnyvale is on-site CA, not in radius.
        self.assertFalse(self.keep("Site Reliability Engineer", "Sunnyvale, CA"))

    def test_foreign_location_dropped(self):
        self.assertFalse(self.keep("Site Reliability Engineer", "Bangkok, th"))

    def test_hr_director_dropped_no_title_term(self):
        self.assertFalse(self.keep("Senior Director, HR Business Partnering", "Remote"))

    def test_compensation_director_dropped(self):
        self.assertFalse(self.keep("Equity and Executive Compensation Director", "Remote"))

    def test_logistics_director_dropped(self):
        self.assertFalse(self.keep("Director - Logistics & Supply Chain", "Charlotte, NC"))

    def test_sales_engineer_dropped(self):
        self.assertFalse(self.keep("Sales Engineer, Platform", "Remote"))

    def test_us_does_not_match_inside_houston(self):
        # 'us' as a boundary-matched token must NOT match inside 'Houston'.
        # Removing boundary=True would make this pass-through (wrongly kept).
        lp = jm.compile_patterns(["us"], boundary=True)
        tp = jm.compile_patterns(["SRE"])
        job = {"title": "SRE", "location": "Houston, TX"}
        self.assertFalse(jm.matches(job, tp, lp, []))

    # --- foreign-location guard ---
    def test_foreign_remote_in_title_dropped(self):
        # Title says Remote but location is Ireland -> not US-eligible, drop.
        self.assertFalse(self.keep("Senior Site Reliability Engineer, Remote", "Dublin, Ireland"))

    def test_foreign_uk_dropped(self):
        self.assertFalse(self.keep("Platform Engineer (Remote)", "London, UK"))

    def test_foreign_canada_dropped(self):
        self.assertFalse(self.keep("Site Reliability Engineer", "Toronto, Canada"))

    def test_us_remote_kept_despite_remote_word(self):
        # Explicit US signal in location -> keep.
        self.assertTrue(self.keep("Site Reliability Engineer, Remote", "Remote - US"))

    def test_bare_remote_kept(self):
        # No foreign marker, bare remote -> assume US-eligible, keep.
        self.assertTrue(self.keep("Platform Engineer", "Remote"))

    def test_dublin_ca_kept(self):
        # Dublin, CA is a US tech hub; must NOT be dropped by the foreign guard.
        self.assertTrue(self.keep("Platform Engineer", "Dublin, CA"))

    def test_vancouver_wa_kept(self):
        self.assertTrue(self.keep("Site Reliability Engineer", "Vancouver, WA"))

    def test_new_mexico_kept(self):
        # "New Mexico" is a US state; must not match the foreign "mexico" marker.
        self.assertTrue(self.keep("Site Reliability Engineer", "Albuquerque, New Mexico"))

    # --- hardware / manufacturing exclusions (Anduril noise) ---
    def test_manufacturing_eng_mgr_dropped(self):
        self.assertFalse(self.keep("Manufacturing Engineering Manager", "Remote"))

    def test_rocket_motor_dropped(self):
        self.assertFalse(self.keep("Director of Engineering, Rocket Motor Systems", "Remote"))

    def test_missiles_reliability_dropped(self):
        self.assertFalse(self.keep("Manager, Missiles Reliability Engineering", "Remote"))

    def test_flight_software_dropped(self):
        self.assertFalse(self.keep("Senior Flight Software Platform Engineer", "Remote"))

    def test_embedded_systems_dropped(self):
        self.assertFalse(self.keep("Engineering Manager, Embedded Systems Engineering", "Remote"))

    def test_propulsion_dropped(self):
        self.assertFalse(self.keep("Propulsion Test Infrastructure Engineer", "Remote"))

    # --- onsite-metro QUARANTINE (Fix 2): "City, State, United States" is kept
    #     but classified 'verify' so it stays out of default views. The ATS
    #     location field can mislabel a remote role as a primary office, so we
    #     never silently drop these; a human checks the JD. ---
    def classify(self, title, loc):
        return jm.classify_location(title, loc, self.lp)

    def test_onsite_metro_kept_and_flagged_verify(self):
        self.assertTrue(self.keep("Senior Solutions Architect",
                                  "San Francisco, California, United States"))
        self.assertEqual(self.classify("Senior Solutions Architect",
                                        "San Francisco, California, United States"),
                         "verify")

    def test_usa_onsite_metro_kept_and_verify(self):
        # "USA" (not "United States") is not in location_terms, but the role must
        # still be kept and quarantined, not silently dropped before classify.
        self.assertTrue(self.keep("Solutions Architect", "San Francisco, CA, USA"))
        self.assertEqual(self.classify("Solutions Architect", "San Francisco, CA, USA"),
                         "verify")

    def test_us_dotted_onsite_metro_kept_and_verify(self):
        # "U.S." ends in a dot; the country pattern must still match it (trailing
        # \b would fail at end-of-string) so the role is kept and quarantined.
        self.assertTrue(self.keep("Solutions Architect", "San Francisco, CA, U.S."))
        self.assertEqual(self.classify("Solutions Architect", "San Francisco, CA, U.S."),
                         "verify")

    def test_onsite_metro_multi_city_flagged_verify(self):
        self.assertEqual(self.classify("Head of AI Platform Engineering",
                                       "Cupertino, California, United States"),
                         "verify")

    def test_bare_country_is_remote(self):
        # Whole-country location with no specific metro reads as remote-US.
        self.assertTrue(self.keep("Solutions Architect", "United States"))
        self.assertEqual(self.classify("Solutions Architect", "United States"), "remote")

    def test_regional_metro_with_country_not_verify(self):
        # Hillsboro is in-radius; kept and NOT quarantined.
        self.assertTrue(self.keep("Platform Engineer", "Hillsboro, OR, United States"))
        self.assertNotEqual(self.classify("Platform Engineer",
                                          "Hillsboro, OR, United States"), "verify")

    def test_multi_region_remote_kept_not_verify(self):
        # "United States, Canada" names no US state -> multi-region, kept + visible.
        self.assertTrue(self.keep("Sr. Engineering Manager, Cloud", "United States, Canada"))
        self.assertNotEqual(self.classify("Sr. Engineering Manager, Cloud",
                                          "United States, Canada"), "verify")

    def test_dotted_us_multi_region_kept(self):
        # "U.S., Canada" must be kept like "United States, Canada": the foreign
        # guard's US check has to recognize the dotted "U.S." marker.
        self.assertTrue(self.keep("Solutions Architect", "U.S., Canada"))
        self.assertNotEqual(self.classify("Solutions Architect", "U.S., Canada"), "verify")

    def test_multi_region_with_or_not_verify(self):
        # "or Canada" must not let the code path match "OR".
        self.assertTrue(self.keep("Solutions Architect", "United States or Canada"))
        self.assertNotEqual(self.classify("Solutions Architect",
                                          "United States or Canada"), "verify")

    def test_lowercase_state_code_flagged_verify(self):
        # Lowercase "tx" in City, ST position is still an onsite metro -> verify.
        self.assertEqual(self.classify("Platform Engineer", "Austin, tx, United States"),
                         "verify")

    # --- title-only location terms must not fake remote (Fix 1) ---
    def test_distributed_in_title_is_not_remote(self):
        # "Distributed" in the title is a system descriptor, not a location; the
        # role matches a title term ("Platform Engineer") but is SF-onsite.
        self.assertFalse(self.keep("Distributed Systems Platform Engineer",
                                   "San Francisco"))

    def test_remote_word_in_title_kept(self):
        # An explicit "Remote" in the title still counts when location is blank.
        self.assertTrue(self.keep("Senior Site Reliability Engineer - Remote", ""))

    # --- foreign-city guard (Fix 3) ---
    def test_foreign_city_buenos_aires_dropped(self):
        self.assertFalse(self.keep("DevOps Support Engineer", "Remote (Buenos Aires)"))

    def test_foreign_city_bengaluru_dropped(self):
        self.assertFalse(self.keep("Platform Engineer", "Remote - Bengaluru"))

    # --- structural exclude (Fix 4) ---
    def test_structural_design_em_dropped(self):
        self.assertFalse(self.keep("Engineering Manager, Structural Design", "United States"))

    def test_infrastructure_not_caught_by_structural(self):
        # "structural" must not collide with "infrastructure".
        self.assertTrue(self.keep("Senior Infrastructure Engineer", "Remote"))

    # --- must still KEEP legit software roles ---
    def test_sre_still_kept(self):
        self.assertTrue(self.keep("Senior Site Reliability Engineer", "Remote"))

    def test_data_platform_engineer_kept(self):
        self.assertTrue(self.keep("Senior Data Platform Engineer", "Remote"))

    def test_devops_engineer_kept(self):
        self.assertTrue(self.keep("DevOps Engineer", "United States"))

    def test_mission_software_infra_kept(self):
        self.assertTrue(self.keep("Mission Software Infrastructure Engineer", "Remote"))

    def test_production_engineer_kept(self):
        # software "production engineer" is SRE-adjacent; only manufacturing variants drop
        self.assertTrue(self.keep("Senior Production Engineer", "Remote"))

    # --- multi-country REGION names (the open-jobs wide net) ---------------
    # FOREIGN_MARKERS listed countries and cities only, which was enough while
    # discovery read a curated list of US-centric boards. The wide net reads a
    # global corpus, where a posting's whole location field is often just the
    # region: "Europe, Remote", "Remote - EMEA", "LATAM". Those name no country
    # and no city, so nothing caught them.
    def test_region_only_remote_is_dropped(self):
        self.assertFalse(self.keep("Senior Platform Engineer", "Europe, Remote"))
        self.assertFalse(self.keep("Cloud Engineer", "Europe (Remote)"))
        self.assertFalse(self.keep("DevOps Engineer", "Remote - EMEA"))
        self.assertFalse(self.keep("Site Reliability Engineer", "Remote, LATAM"))
        self.assertFalse(self.keep("Platform Engineer", "APAC"))

    def test_a_region_that_also_names_the_us_is_kept(self):
        # "Remote: United States | Canada | United Kingdom" is a real posting
        # shape that is eligible. The guard drops a location only when no US
        # marker is present anywhere in it.
        self.assertTrue(self.keep(
            "Senior Platform Engineer",
            "Remote: United States | Canada | United Kingdom"))
        self.assertTrue(self.keep("Cloud Engineer", "United States & Canada"))

    def test_a_multi_region_listing_that_includes_the_americas_is_kept(self):
        # Regression from adding "europe": "Americas" covers the US but was not
        # a US marker, so a US-eligible listing was dropped by the foreign
        # guard. Verified against the real phrasing on live postings.
        self.assertTrue(self.keep("Cloud Engineer", "Remote - Americas and Europe"))
        self.assertTrue(self.keep("Cloud Engineer", "Remote, Americas"))

    def test_americas_still_vouches_beside_any_region_word(self):
        # Every region marker names territory that EXCLUDES the US, so none of
        # them is evidence against "Americas". Only a named country or city is.
        for region in ("Europe", "European Union", "EMEA", "APAC"):
            self.assertTrue(
                self.keep("Cloud Engineer", f"Remote - Americas and {region}"),
                region)

    def test_americas_does_not_override_a_named_foreign_country(self):
        # "Americas" was first added as a plain US marker, and matches() short
        # circuits the foreign guard the moment ANY US marker appears. That let
        # a listing naming Brazil and Mexico through, which is worse than the
        # regression it was fixing. "Americas" now only vouches for US
        # eligibility when no specific foreign country or city is named.
        self.assertFalse(self.keep(
            "Cloud Engineer", "Remote - Americas (Brazil, Mexico, Argentina)"))
        self.assertFalse(self.keep("Cloud Engineer", "Remote - Americas, Bengaluru"))

    def test_latam_is_the_non_us_americas_and_still_excludes(self):
        # LATAM names the Americas MINUS the US, so "Americas" beside it is not
        # evidence of US eligibility.
        self.assertFalse(self.keep("Cloud Engineer", "Remote - LATAM / Americas"))
        self.assertFalse(self.keep("Site Reliability Engineer", "Remote, LATAM"))

    def test_an_explicit_us_marker_still_wins_over_everything(self):
        # A real posting that names the US outright is eligible no matter what
        # else it lists.
        self.assertTrue(self.keep(
            "Cloud Engineer", "Remote: United States | Canada | Brazil"))

    def test_region_words_do_not_collide_with_us_place_names(self):
        # The markers are word-bounded, so no US place name may be swallowed by
        # a region token. "Apache Junction" is the live case for "apac".
        self.assertTrue(self.keep("Cloud Engineer", "Remote, United States"))
        self.assertTrue(self.keep("Platform Engineer", "Apache Junction, AZ, USA"))

    def test_the_european_union_spelled_out_is_still_foreign(self):
        # "\beurope\b" does not match "European", so "European Union, Remote"
        # and "Remote - EU" both leaked through: exactly the roles the region
        # marker was added to catch. A bare "eu" is too collision-prone to add
        # (it appears inside ordinary words), so the spelled-out forms are
        # matched instead.
        self.assertFalse(self.keep("Cloud Engineer", "European Union, Remote"))
        self.assertFalse(self.keep("Platform Engineer", "Remote, Europe (EU)"))


if __name__ == "__main__":
    unittest.main()
