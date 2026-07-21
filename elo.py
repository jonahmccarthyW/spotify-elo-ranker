def get_expected_score(rating_a, rating_b):
    """
    Returns the probability of A winning against B.
    """
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))


def get_k_factor(matches_played):
    """
    Returns a higher K-factor for placement matches (< 5).
    """
    if matches_played < 5:
        return 64  # High volatility for new songs
    return 32  # Standard stability


def calculate_new_ratings(rating_a, rating_b, actual_score_a, matches_a, matches_b):
    """
    Updates ratings for both players using dynamic K-factors.
    """
    expected_a = get_expected_score(rating_a, rating_b)
    expected_b = 1 - expected_a

    # Determine K-factor for each song independently
    k_a = get_k_factor(matches_a)
    k_b = get_k_factor(matches_b)

    # Calculate updates
    new_rating_a = rating_a + k_a * (actual_score_a - expected_a)

    # actual_score_b is (1 - actual_score_a)
    actual_score_b = 1 - actual_score_a
    new_rating_b = rating_b + k_b * (actual_score_b - expected_b)

    return round(new_rating_a, 2), round(new_rating_b, 2)

def preview_deltas(rating_a, rating_b, matches_a, matches_b):
    """
    Returns the Elo swing each song would see for each outcome, without
    mutating anything. Used to show a "+34 / -28" preview before voting.

      a_win / a_lose  -> change to song A if it wins / loses
      b_win / b_lose  -> change to song B if it wins / loses
    """
    expected_a = get_expected_score(rating_a, rating_b)
    expected_b = 1 - expected_a
    k_a = get_k_factor(matches_a)
    k_b = get_k_factor(matches_b)

    return {
        'a_win':  round(k_a * (1 - expected_a)),
        'a_lose': round(k_a * (0 - expected_a)),   # already negative
        'b_win':  round(k_b * (1 - expected_b)),
        'b_lose': round(k_b * (0 - expected_b)),   # already negative
    }