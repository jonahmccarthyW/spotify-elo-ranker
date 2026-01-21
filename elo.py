def get_expected_score(rating_a, rating_b):
    """
    Returns the probability of A winning against B.
    """
    return 1 / (1 + 10 ** ((rating_b - rating_a) / 400))


def calculate_new_ratings(rating_a, rating_b, actual_score_a, k=32):
    """
    Updates ratings for both players.
    actual_score_a: 1 (Win), 0 (Loss), 0.5 (Draw)
    """
    expected_a = get_expected_score(rating_a, rating_b)

    # Update A
    new_rating_a = rating_a + k * (actual_score_a - expected_a)

    # Update B (Note: actual_score_b is 1 - actual_score_a)
    actual_score_b = 1 - actual_score_a
    expected_b = 1 - expected_a  # or get_expected_score(rating_b, rating_a)
    new_rating_b = rating_b + k * (actual_score_b - expected_b)

    return round(new_rating_a, 2), round(new_rating_b, 2)