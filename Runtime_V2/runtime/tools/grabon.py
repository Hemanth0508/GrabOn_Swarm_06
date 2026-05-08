"""
runtime/tools/grabon.py
Deterministic GrabOn coupon dataset used for governed Event 19 grounding.
"""


def get_grabon_coupons():
    return [
        {
            "merchant": "Myntra",
            "discount": "70% OFF",
            "category": "Fashion",
            "verified": True,
            "confidence": 0.94,
        },
        {
            "merchant": "Ajio",
            "discount": "60% OFF",
            "category": "Fashion",
            "verified": True,
            "confidence": 0.92,
        },
        {
            "merchant": "Nykaa",
            "discount": "Flat ₹500 OFF",
            "category": "Beauty",
            "verified": True,
            "confidence": 0.91,
        },
        {
            "merchant": "Puma",
            "discount": "55% OFF",
            "category": "Fashion",
            "verified": True,
            "confidence": 0.89,
        },
        {
            "merchant": "Boat",
            "discount": "Up to 65% OFF",
            "category": "Electronics",
            "verified": True,
            "confidence": 0.90,
        },
        {
            "merchant": "Swiggy",
            "discount": "Flat ₹125 OFF",
            "category": "Food",
            "verified": False,
            "confidence": 0.62,
        },
        {
            "merchant": "Zomato",
            "discount": "50% OFF up to ₹100",
            "category": "Food",
            "verified": True,
            "confidence": 0.87,
        },
        {
            "merchant": "MakeMyTrip",
            "discount": "Flat ₹2000 OFF Flights",
            "category": "Travel",
            "verified": True,
            "confidence": 0.93,
        },
        {
            "merchant": "Flipkart",
            "discount": "80% OFF Electronics",
            "category": "Electronics",
            "verified": True,
            "confidence": 0.90,
        },
        {
            "merchant": "PharmEasy",
            "discount": "25% OFF Medicines",
            "category": "Health",
            "verified": False,
            "confidence": 0.66,
        },
    ]

