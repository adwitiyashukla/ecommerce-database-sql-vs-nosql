import csv
import hashlib
import json
from datetime import date, datetime, timedelta

import numpy as np

VERSION = 1

TABLES = {
    "categories": ("category_id", "parent_id", "name"),
    "sellers": ("seller_id", "name", "state", "rating", "joined_at"),
    "customers": ("customer_id", "email", "name", "state", "zip", "created_at"),
    "products": ("product_id", "category_id", "title", "brand", "min_price_cents", "created_at"),
    "skus": ("sku_id", "product_id", "variant"),
    "offers": ("offer_id", "sku_id", "seller_id", "price_cents", "list_price_cents", "available"),
    "cart_items": ("customer_id", "offer_id", "qty", "added_at"),
    "orders": (
        "order_id",
        "customer_id",
        "status",
        "created_at",
        "item_count",
        "total_cents",
        "ship_state",
        "ship_zip",
    ),
    "order_items": (
        "order_id",
        "line_no",
        "offer_id",
        "seller_id",
        "qty",
        "unit_price_cents",
        "created_at",
    ),
    "shipments": ("order_id", "carrier", "tracking", "shipped_at", "delivered_at"),
    "reviews": ("review_id", "product_id", "customer_id", "rating", "title", "created_at"),
}

STATES = (
    ("AL", 5.0, ((350, 369),)),
    ("AK", 0.7, ((995, 999),)),
    ("AZ", 7.2, ((850, 865),)),
    ("AR", 3.0, ((716, 729),)),
    ("CA", 39.5, ((900, 961),)),
    ("CO", 5.8, ((800, 816),)),
    ("CT", 3.6, ((60, 69),)),
    ("DE", 1.0, ((197, 199),)),
    ("DC", 0.7, ((200, 205),)),
    ("FL", 21.5, ((320, 349),)),
    ("GA", 10.7, ((300, 319), (398, 399))),
    ("HI", 1.5, ((967, 968),)),
    ("ID", 1.8, ((832, 838),)),
    ("IL", 12.8, ((600, 629),)),
    ("IN", 6.8, ((460, 479),)),
    ("IA", 3.2, ((500, 528),)),
    ("KS", 2.9, ((660, 679),)),
    ("KY", 4.5, ((400, 427),)),
    ("LA", 4.7, ((700, 714),)),
    ("ME", 1.4, ((39, 49),)),
    ("MD", 6.2, ((206, 219),)),
    ("MA", 7.0, ((10, 27),)),
    ("MI", 10.1, ((480, 499),)),
    ("MN", 5.7, ((550, 567),)),
    ("MS", 3.0, ((386, 397),)),
    ("MO", 6.2, ((630, 658),)),
    ("MT", 1.1, ((590, 599),)),
    ("NE", 2.0, ((680, 693),)),
    ("NV", 3.1, ((889, 898),)),
    ("NH", 1.4, ((30, 38),)),
    ("NJ", 9.3, ((70, 89),)),
    ("NM", 2.1, ((870, 884),)),
    ("NY", 20.2, ((100, 149),)),
    ("NC", 10.4, ((270, 289),)),
    ("ND", 0.8, ((580, 588),)),
    ("OH", 11.8, ((430, 459),)),
    ("OK", 4.0, ((730, 749),)),
    ("OR", 4.2, ((970, 979),)),
    ("PA", 13.0, ((150, 196),)),
    ("RI", 1.1, ((28, 29),)),
    ("SC", 5.1, ((290, 299),)),
    ("SD", 0.9, ((570, 577),)),
    ("TN", 6.9, ((370, 385),)),
    ("TX", 29.1, ((750, 799), (885, 885))),
    ("UT", 3.3, ((840, 847),)),
    ("VT", 0.6, ((50, 59),)),
    ("VA", 8.6, ((220, 246),)),
    ("WA", 7.7, ((980, 994),)),
    ("WV", 1.8, ((247, 268),)),
    ("WI", 5.9, ((530, 549),)),
    ("WY", 0.6, ((820, 831),)),
)

FIRST = (
    "James Mary Robert Patricia John Jennifer Michael Linda David Elizabeth William "
    "Barbara Richard Susan Joseph Jessica Thomas Sarah Charles Karen Christopher Lisa "
    "Daniel Nancy Matthew Emily Anthony Sandra Mark Ashley Steven Kimberly Andrew "
    "Michelle Joshua Amanda Kevin Melissa Brian Stephanie Ryan Rebecca Jacob Laura "
    "Nicholas Olivia Ethan Sophia Priya Arjun Wei Mei Carlos Maria Jose Ana Luis Fatima "
    "Omar Grace "
).split()

LAST = (
    "Smith Johnson Williams Brown Jones Garcia Miller Davis Rodriguez Martinez Hernandez "
    "Lopez Gonzalez Wilson Anderson Thomas Taylor Moore Jackson Martin Lee Perez "
    "Thompson White Harris Sanchez Clark Ramirez Lewis Robinson Walker Young Allen King "
    "Wright Scott Torres Nguyen Hill Flores Green Adams Nelson Baker Hall Rivera "
    "Campbell Mitchell Carter Roberts Patel Shah Kim Chen Singh "
).split()

SELLER_A = (
    "Maple Cedar Harbor Summit Prairie River Granite Lakeside Pine Copper Silver Golden "
    "Blue Red Green Northern Southern Eastern Western Coastal Valley Canyon Meadow Ridge "
    "Bay Oak Willow Aspen Cypress Sierra Liberty Union Pioneer Frontier Heritage Atlas "
    "Beacon Compass Anchor Keystone "
).split()

SELLER_B = (
    "Goods Supply Outlet Trading Market Depot Emporium Mercantile Store Shop Exchange "
    "Warehouse Direct Wholesale Bazaar Corner Collective House Works Provisions "
).split()

SELLER_C = ("Co", "LLC", "Inc", "Group", "and Sons", "Partners")

BRAND_HEAD = (
    "Vel Quin Bright Nor Alta Cres Ever Lumi Terra Zen Ori Mira Sol Nova Kin Ash Bel Cor "
    "Dal Ely Fen Gal Hal Iso Jun Kay Lor Mon Nim Oak "
).split()

BRAND_TAIL = (
    "ora tek way line field co ra ix io on ia ex ly vo mark craft lab nest peak path "
).split()

ADJECTIVES = (
    "Classic",
    "Premium",
    "Essential",
    "Compact",
    "Deluxe",
    "Everyday",
    "Pro",
    "Ultra",
    "Eco",
    "Signature",
    "Lightweight",
    "Heavy Duty",
    "Soft",
    "Smart",
    "Portable",
    "Original",
    "Advanced",
    "Basic",
    "Studio",
    "Travel",
)

VARIANTS = {
    "color": ("Black", "White", "Gray", "Navy", "Red", "Blue", "Green", "Silver"),
    "size": ("XS", "S", "M", "L", "XL", "XXL"),
    "format": ("Hardcover", "Paperback", "Audiobook", "Ebook"),
    "pack": ("Single", "2-Pack", "4-Pack", "12-Pack"),
    "volume": ("50 ml", "100 ml", "200 ml", "500 ml"),
}

SKU_RANGE = {"color": (1, 3), "size": (3, 6), "format": (1, 3), "pack": (1, 3), "volume": (1, 3)}

DEPARTMENTS = (
    (
        "Electronics",
        0.16,
        "color",
        (
            (
                "Headphones",
                ("Wireless Headphones", "Over-Ear Headphones", "Studio Headphones"),
                20,
                300,
            ),
            ("Earbuds", ("Wireless Earbuds", "Sport Earbuds", "Kids Earbuds"), 15, 200),
            ("Speakers", ("Bluetooth Speaker", "Smart Speaker", "Soundbar"), 25, 400),
            ("Phone Accessories", ("Phone Case", "Screen Protector", "Car Mount"), 8, 60),
            ("Chargers and Cables", ("USB-C Cable", "Wall Charger", "Power Bank"), 8, 80),
            ("Smart Watches", ("Smart Watch", "Fitness Tracker", "Watch Band"), 20, 350),
        ),
    ),
    (
        "Home and Kitchen",
        0.14,
        "color",
        (
            ("Cookware", ("Nonstick Skillet", "Dutch Oven", "Saucepan Set"), 20, 250),
            ("Small Appliances", ("Air Fryer", "Blender", "Toaster"), 25, 300),
            ("Bedding", ("Sheet Set", "Comforter", "Pillow"), 20, 200),
            ("Storage", ("Storage Bins", "Shoe Rack", "Closet Organizer"), 10, 120),
            ("Lighting", ("Desk Lamp", "LED Strip Lights", "Floor Lamp"), 15, 150),
            ("Coffee and Tea", ("Pour Over Kettle", "French Press", "Coffee Grinder"), 15, 180),
        ),
    ),
    (
        "Clothing",
        0.15,
        "size",
        (
            ("Men's T-Shirts", ("Crew Neck T-Shirt", "V-Neck T-Shirt", "Graphic Tee"), 8, 40),
            ("Women's Dresses", ("Wrap Dress", "Maxi Dress", "Shirt Dress"), 25, 120),
            ("Jackets", ("Rain Jacket", "Puffer Jacket", "Fleece Jacket"), 30, 200),
            ("Jeans", ("Slim Fit Jeans", "Relaxed Fit Jeans", "Straight Leg Jeans"), 25, 90),
            ("Sneakers", ("Running Shoes", "Walking Shoes", "Canvas Sneakers"), 30, 160),
            ("Socks", ("Ankle Socks", "Crew Socks", "Wool Socks"), 6, 30),
        ),
    ),
    (
        "Books",
        0.09,
        "format",
        (
            ("Fiction", ("Mystery Novel", "Fantasy Novel", "Thriller"), 8, 35),
            ("Cookbooks", ("Weeknight Cookbook", "Baking Book", "Grilling Guide"), 12, 40),
            ("Science", ("Physics Primer", "Astronomy Guide", "Biology Handbook"), 12, 60),
            ("Children's Books", ("Picture Book", "Bedtime Stories", "Activity Book"), 6, 25),
            ("Biographies", ("Memoir", "Biography", "Autobiography"), 10, 40),
            ("Self Help", ("Habits Guide", "Productivity Book", "Mindfulness Book"), 10, 30),
        ),
    ),
    (
        "Toys and Games",
        0.08,
        "color",
        (
            ("Board Games", ("Strategy Game", "Party Game", "Family Game"), 15, 70),
            ("Puzzles", ("1000 Piece Puzzle", "Brain Teaser", "Kids Puzzle"), 8, 40),
            ("Building Sets", ("Building Blocks", "Construction Set", "Robot Kit"), 15, 150),
            ("Plush Toys", ("Teddy Bear", "Plush Dinosaur", "Stuffed Bunny"), 8, 40),
            ("Outdoor Play", ("Water Blaster", "Kite", "Jump Rope"), 8, 60),
            ("Card Games", ("Card Game", "Trivia Cards", "Word Game"), 6, 25),
        ),
    ),
    (
        "Sports and Outdoors",
        0.09,
        "color",
        (
            ("Camping", ("Camping Tent", "Sleeping Bag", "Camp Chair"), 25, 300),
            ("Fitness", ("Dumbbell Set", "Resistance Bands", "Kettlebell"), 15, 200),
            ("Cycling", ("Bike Helmet", "Bike Light", "Bike Lock"), 15, 120),
            ("Water Bottles", ("Insulated Bottle", "Sports Bottle", "Hydration Pack"), 10, 60),
            ("Yoga", ("Yoga Mat", "Yoga Blocks", "Yoga Strap"), 10, 90),
            ("Hiking", ("Trekking Poles", "Daypack", "Hiking Socks"), 12, 150),
        ),
    ),
    (
        "Beauty",
        0.08,
        "volume",
        (
            ("Skin Care", ("Face Moisturizer", "Vitamin C Serum", "Sunscreen"), 8, 80),
            ("Hair Care", ("Shampoo", "Conditioner", "Hair Oil"), 6, 40),
            ("Makeup", ("Foundation", "Mascara", "Lip Gloss"), 6, 50),
            ("Fragrance", ("Eau de Parfum", "Body Mist", "Cologne"), 15, 120),
            ("Bath and Body", ("Body Wash", "Hand Cream", "Bath Salts"), 6, 35),
            ("Nail Care", ("Nail Polish", "Cuticle Oil", "Nail Kit"), 5, 30),
        ),
    ),
    (
        "Grocery",
        0.08,
        "pack",
        (
            ("Snacks", ("Trail Mix", "Protein Bars", "Popcorn"), 4, 30),
            ("Coffee", ("Ground Coffee", "Coffee Pods", "Whole Bean Coffee"), 8, 40),
            ("Pantry", ("Olive Oil", "Pasta", "Rice"), 3, 30),
            ("Beverages", ("Sparkling Water", "Green Tea", "Juice Boxes"), 4, 30),
            ("Breakfast", ("Granola", "Oatmeal", "Pancake Mix"), 4, 20),
            ("Candy", ("Chocolate Bar", "Gummy Bears", "Mints"), 3, 20),
        ),
    ),
    (
        "Pet Supplies",
        0.07,
        "pack",
        (
            ("Dog Food", ("Dry Dog Food", "Dog Treats", "Puppy Food"), 10, 90),
            ("Cat Supplies", ("Cat Litter", "Cat Food", "Scratching Post"), 8, 70),
            ("Pet Toys", ("Chew Toy", "Feather Wand", "Squeaky Ball"), 4, 30),
            ("Aquarium", ("Aquarium Filter", "Fish Food", "Water Conditioner"), 5, 120),
            ("Pet Beds", ("Orthopedic Dog Bed", "Cat Bed", "Pet Blanket"), 20, 150),
            ("Bird Supplies", ("Bird Seed", "Bird Cage", "Bird Feeder"), 6, 120),
        ),
    ),
    (
        "Office Products",
        0.06,
        "color",
        (
            ("Notebooks", ("Spiral Notebook", "Composition Book", "Dot Grid Journal"), 4, 25),
            ("Pens", ("Gel Pens", "Ballpoint Pens", "Fine Liners"), 4, 30),
            ("Desk Organizers", ("Desk Organizer", "File Sorter", "Pen Holder"), 8, 60),
            ("Paper", ("Printer Paper", "Sticky Notes", "Index Cards"), 4, 50),
            ("Planners", ("Weekly Planner", "Wall Calendar", "Academic Planner"), 6, 35),
            ("Labels", ("Label Maker", "Shipping Labels", "File Labels"), 5, 80),
        ),
    ),
)

WEEKDAY = (1.0, 0.98, 0.97, 0.98, 1.0, 1.06, 1.1)

HOUR = (
    0.9,
    1.0,
    1.0,
    0.9,
    0.7,
    0.5,
    0.35,
    0.25,
    0.2,
    0.2,
    0.3,
    0.45,
    0.6,
    0.75,
    0.85,
    0.9,
    0.95,
    1.0,
    1.0,
    1.0,
    0.95,
    0.9,
    0.85,
    0.85,
)

REVIEW_TITLES = {
    1: ("Stopped working", "Not as described", "Would not buy again"),
    2: ("Disappointed", "Could be better", "Cheaply made"),
    3: ("It is okay", "Average", "Mixed feelings", "Not bad"),
    4: ("Pretty good", "Solid purchase", "Good for the price", "Does the job"),
    5: ("Love it", "Works great", "Exactly as described", "Great value", "Would buy again"),
}

CARRIERS = (("UPS", "1Z", 0.35), ("USPS", "94", 0.30), ("FedEx", "77", 0.25), ("DHL", "JD", 0.10))

STATUSES = ("pending", "paid", "shipped", "delivered", "cancelled", "returned")

DAY = 86400


def nth_weekday(year, month, weekday, n):
    first = date(year, month, 1)
    return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))


def sale_events(year):
    summer = nth_weekday(year, 7, 1, 2)
    thanksgiving = nth_weekday(year, 11, 3, 4)
    return {
        "summer_start": summer,
        "summer_end": summer + timedelta(days=3),
        "black_friday": thanksgiving + timedelta(days=1),
        "cyber_monday": thanksgiving + timedelta(days=4),
        "thanksgiving": thanksgiving,
    }


def day_weights(start, days):
    weights = np.empty(days)
    for i in range(days):
        day = start + timedelta(days=i)
        events = sale_events(day.year)
        weight = (1.0 + 0.15 * i / days) * WEEKDAY[day.weekday()]
        if events["summer_start"] <= day <= events["summer_end"]:
            weight *= 4.0
        elif day == events["black_friday"]:
            weight *= 5.0
        elif day == events["cyber_monday"]:
            weight *= 4.0
        elif events["thanksgiving"] - timedelta(days=3) <= day <= events["cyber_monday"]:
            weight *= 1.6
        elif date(day.year, 12, 2) <= day <= date(day.year, 12, 20):
            weight *= 1.5
        elif date(day.year, 12, 24) <= day <= date(day.year, 12, 31):
            weight *= 0.8
        weights[i] = weight
    return weights


def sizes(scale):
    return {
        "customers": max(500, round(100_000 * scale)),
        "sellers": max(30, round(2_000 * scale)),
        "brands": min(len(BRAND_HEAD) * len(BRAND_TAIL), max(40, round(400 * scale))),
        "products": max(300, round(25_000 * scale)),
        "orders": max(1_000, round(500_000 * scale)),
    }


def stamp(seconds):
    values = np.asarray(seconds, dtype="int64").astype("datetime64[s]")
    return np.datetime_as_string(values, unit="s").tolist()


def round_99(cents):
    return np.maximum(99, np.rint(cents / 100.0).astype(np.int64) * 100 - 1)


def write_table(out, name, columns):
    path = out / f"{name}.csv"
    rows = 0
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(TABLES[name])
        for row in zip(*columns, strict=True):
            writer.writerow(row)
            rows += 1
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"rows": rows, "sha256": digest}


def params_for(cfg, low_stock):
    return {
        "version": VERSION,
        "seed": cfg.seed,
        "scale": cfg.scale,
        "start_date": cfg.start_date.isoformat(),
        "days": cfg.days,
        "low_stock": low_stock,
    }


def read_manifest(out):
    path = out / "manifest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def up_to_date(cfg, low_stock):
    manifest = read_manifest(cfg.out_dir)
    if manifest is None or manifest.get("params") != params_for(cfg, low_stock):
        return False
    return all((cfg.out_dir / f"{name}.csv").exists() for name in TABLES)


def leaf_catalog():
    leaves = []
    rows = []
    for d, (dept, dept_weight, kind, children) in enumerate(DEPARTMENTS, start=1):
        rows.append((d, "", dept))
        for j, (name, nouns, lo, hi) in enumerate(children, start=1):
            leaf_id = d * 100 + j
            rows.append((leaf_id, d, name))
            leaves.append((leaf_id, dept_weight / len(children), kind, nouns, lo, hi))
    return rows, leaves


def generate(cfg, low_stock, force=False):
    out = cfg.out_dir
    if not force and up_to_date(cfg, low_stock):
        return read_manifest(out)
    out.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(cfg.seed)
    n = sizes(cfg.scale)
    start = datetime.combine(cfg.start_date, datetime.min.time())
    t0 = int((start - datetime(1970, 1, 1)).total_seconds())
    end = t0 + cfg.days * DAY
    tables = {}

    category_rows, leaves = leaf_catalog()
    tables["categories"] = write_table(out, "categories", list(zip(*category_rows, strict=True)))

    state_codes = [s[0] for s in STATES]
    state_p = np.array([s[1] for s in STATES])
    state_p /= state_p.sum()

    def zips_for(states):
        result = np.empty(len(states), dtype=object)
        for s, (_, _, ranges) in enumerate(STATES):
            idx = np.nonzero(states == s)[0]
            prefixes = np.concatenate([np.arange(lo, hi + 1) for lo, hi in ranges])
            heads = rng.choice(prefixes, size=len(idx))
            tails = rng.integers(0, 100, size=len(idx))
            result[idx] = [
                f"{h:03d}{t:02d}" for h, t in zip(heads.tolist(), tails.tolist(), strict=True)
            ]
        return result.tolist()

    seller_names = [f"{a} {b} {c}" for a in SELLER_A for b in SELLER_B for c in SELLER_C]
    order_idx = rng.permutation(len(seller_names))
    names = []
    for i in range(n["sellers"]):
        base = seller_names[int(order_idx[i % len(seller_names)])]
        names.append(base if i < len(seller_names) else f"{base} {i // len(seller_names) + 1}")
    seller_state = rng.choice(len(STATES), size=n["sellers"], p=state_p)
    ratings = np.round(rng.beta(5.0, 1.5, size=n["sellers"]) * 4.0 + 1.0, 1)
    seller_joined = t0 - rng.integers(30 * DAY, 2000 * DAY, size=n["sellers"])
    tables["sellers"] = write_table(
        out,
        "sellers",
        [
            range(1, n["sellers"] + 1),
            names,
            [state_codes[s] for s in seller_state.tolist()],
            [f"{r:.1f}" for r in ratings.tolist()],
            stamp(seller_joined),
        ],
    )

    first = rng.integers(len(FIRST), size=n["customers"])
    last = rng.integers(len(LAST), size=n["customers"])
    cust_state = rng.choice(len(STATES), size=n["customers"], p=state_p)
    cust_zip = zips_for(cust_state)
    cust_created = t0 - rng.integers(DAY, 1500 * DAY, size=n["customers"])
    cust_names = [
        f"{FIRST[a]} {LAST[b]}" for a, b in zip(first.tolist(), last.tolist(), strict=True)
    ]
    emails = [
        f"{FIRST[a].lower()}.{LAST[b].lower()}.{i + 1}@example.com"
        for i, (a, b) in enumerate(zip(first.tolist(), last.tolist(), strict=True))
    ]
    cust_state_codes = [state_codes[s] for s in cust_state.tolist()]
    tables["customers"] = write_table(
        out,
        "customers",
        [
            range(1, n["customers"] + 1),
            emails,
            cust_names,
            cust_state_codes,
            cust_zip,
            stamp(cust_created),
        ],
    )

    brands = [h + t for h in BRAND_HEAD for t in BRAND_TAIL]
    brand_pick = rng.permutation(len(brands))[: n["brands"]]
    brands = [brands[int(i)] for i in brand_pick]
    leaf_p = np.array([leaf[1] for leaf in leaves])
    leaf_p /= leaf_p.sum()
    product_leaf = rng.choice(len(leaves), size=n["products"], p=leaf_p)
    product_brand = rng.integers(len(brands), size=n["products"])
    product_adj = rng.integers(len(ADJECTIVES), size=n["products"])
    product_noun = rng.integers(3, size=n["products"])
    lo = np.array([leaves[i][4] for i in product_leaf.tolist()], dtype=float)
    hi = np.array([leaves[i][5] for i in product_leaf.tolist()], dtype=float)
    base_price = np.exp(rng.uniform(np.log(lo), np.log(hi))) * 100.0
    product_created = t0 - rng.integers(DAY, 900 * DAY, size=n["products"])
    titles = [
        f"{brands[b]} {ADJECTIVES[a]} {leaves[f][3][k]}"
        for b, a, f, k in zip(
            product_brand.tolist(),
            product_adj.tolist(),
            product_leaf.tolist(),
            product_noun.tolist(),
            strict=True,
        )
    ]

    sku_product = []
    sku_variant = []
    for p, f in enumerate(product_leaf.tolist()):
        kind = leaves[f][2]
        lo_k, hi_k = SKU_RANGE[kind]
        count = int(rng.integers(lo_k, hi_k + 1))
        options = VARIANTS[kind]
        picks = rng.choice(len(options), size=min(count, len(options)), replace=False)
        for v in sorted(picks.tolist()):
            sku_product.append(p)
            sku_variant.append(options[v])
    sku_product = np.array(sku_product, dtype=np.int64)
    n_skus = len(sku_product)
    sku_start = np.searchsorted(sku_product, np.arange(n["products"]))
    sku_count = np.bincount(sku_product, minlength=n["products"])
    tables["skus"] = write_table(
        out,
        "skus",
        [range(1, n_skus + 1), (sku_product + 1).tolist(), sku_variant],
    )

    seller_pop = 1.0 / np.arange(1, n["sellers"] + 1) ** 0.7
    seller_pop = seller_pop[rng.permutation(n["sellers"])]
    seller_pop /= seller_pop.sum()
    per_sku = 1 + np.minimum(rng.poisson(0.9, size=n_skus), 5)
    candidates = rng.choice(n["sellers"], size=int(per_sku.sum()), p=seller_pop)
    offer_sku = []
    offer_seller = []
    pos = 0
    for s, m in enumerate(per_sku.tolist()):
        seen = []
        for c in candidates[pos : pos + m].tolist():
            if c not in seen:
                seen.append(c)
        pos += m
        for c in sorted(seen):
            offer_sku.append(s)
            offer_seller.append(c)
    offer_sku = np.array(offer_sku, dtype=np.int64)
    offer_seller = np.array(offer_seller, dtype=np.int64)
    n_offers = len(offer_sku)
    offer_price = round_99(base_price[sku_product[offer_sku]] * rng.uniform(0.9, 1.12, n_offers))
    list_price = np.maximum(offer_price, round_99(offer_price * rng.uniform(1.0, 1.35, n_offers)))
    u = rng.random(n_offers)
    healthy = np.maximum(low_stock, np.exp(rng.uniform(np.log(5), np.log(400), n_offers))).astype(
        np.int64
    )
    low = rng.integers(1, max(2, low_stock), size=n_offers)
    available = np.where(u < 0.06, 0, np.where(u < 0.13, np.minimum(low, low_stock - 1), healthy))
    available = np.maximum(available, 0)
    tables["offers"] = write_table(
        out,
        "offers",
        [
            range(1, n_offers + 1),
            (offer_sku + 1).tolist(),
            (offer_seller + 1).tolist(),
            offer_price.tolist(),
            list_price.tolist(),
            available.tolist(),
        ],
    )

    offer_start = np.searchsorted(offer_sku, np.arange(n_skus))
    offer_count = np.bincount(offer_sku, minlength=n_skus)
    order_by_price = np.lexsort((np.arange(n_offers), offer_price, offer_sku))
    cheapest = order_by_price[np.searchsorted(offer_sku[order_by_price], np.arange(n_skus))]
    product_of_offer = sku_product[offer_sku]
    min_price = np.full(n["products"], np.iinfo(np.int64).max, dtype=np.int64)
    np.minimum.at(min_price, product_of_offer, offer_price)
    tables["products"] = write_table(
        out,
        "products",
        [
            range(1, n["products"] + 1),
            [leaves[f][0] for f in product_leaf.tolist()],
            titles,
            [brands[b] for b in product_brand.tolist()],
            min_price.tolist(),
            stamp(product_created),
        ],
    )

    popularity = 1.0 / np.arange(1, n["products"] + 1) ** 0.9
    popularity = popularity[rng.permutation(n["products"])]
    popularity /= popularity.sum()

    def pick_offers(count):
        products = rng.choice(n["products"], size=count, p=popularity)
        skus = sku_start[products] + (rng.random(count) * sku_count[products]).astype(np.int64)
        spread = offer_start[skus] + (rng.random(count) * offer_count[skus]).astype(np.int64)
        return np.where(rng.random(count) < 0.6, cheapest[skus], spread)

    weights = day_weights(cfg.start_date, cfg.days)
    daily = rng.multinomial(n["orders"], weights / weights.sum())
    hour_p = np.array(HOUR) / sum(HOUR)
    created_parts = []
    for i, count in enumerate(daily.tolist()):
        if count == 0:
            continue
        hours = rng.choice(24, size=count, p=hour_p)
        offsets = hours * 3600 + rng.integers(0, 3600, size=count)
        created_parts.append(np.sort(t0 + i * DAY + offsets))
    created = np.concatenate(created_parts)
    n_orders = len(created)
    activity = rng.lognormal(0.0, 1.0, size=n["customers"])
    activity /= activity.sum()
    order_customer = rng.choice(n["customers"], size=n_orders, p=activity)

    lines_per = np.minimum(rng.geometric(0.55, size=n_orders), 8)
    line_order = np.repeat(np.arange(n_orders), lines_per)
    line_offer = pick_offers(len(line_order))
    line_qty = rng.choice(np.array([1, 2, 3]), size=len(line_order), p=[0.85, 0.11, 0.04])
    sort = np.lexsort((line_offer, line_order))
    line_order = line_order[sort]
    line_offer = line_offer[sort]
    line_qty = line_qty[sort]
    keep = np.ones(len(line_order), dtype=bool)
    keep[1:] = (line_order[1:] != line_order[:-1]) | (line_offer[1:] != line_offer[:-1])
    line_order = line_order[keep]
    line_offer = line_offer[keep]
    line_qty = line_qty[keep]
    first_line = np.searchsorted(line_order, np.arange(n_orders))
    line_no = np.arange(len(line_order)) - first_line[line_order] + 1
    item_count = np.bincount(line_order, minlength=n_orders)
    line_price = offer_price[line_offer]
    totals = np.bincount(line_order, weights=line_qty * line_price, minlength=n_orders)
    totals = np.rint(totals).astype(np.int64)

    age = (end - created) / DAY
    u = rng.random(n_orders)
    status = np.empty(n_orders, dtype=np.int64)
    old = age > 14
    mid = (age > 4) & ~old
    recent = (age > 1) & ~old & ~mid
    fresh = age <= 1
    status[old] = np.select([u[old] < 0.93, u[old] < 0.97], [3, 4], 5)
    status[mid] = np.select([u[mid] < 0.70, u[mid] < 0.95], [3, 2], 4)
    status[recent] = np.select([u[recent] < 0.50, u[recent] < 0.90, u[recent] < 0.95], [2, 1, 4], 0)
    status[fresh] = np.select([u[fresh] < 0.60, u[fresh] < 0.95], [1, 0], 4)

    order_ids = np.arange(1, n_orders + 1)
    created_text = stamp(created)
    tables["orders"] = write_table(
        out,
        "orders",
        [
            order_ids.tolist(),
            (order_customer + 1).tolist(),
            [STATUSES[s] for s in status.tolist()],
            created_text,
            item_count.tolist(),
            totals.tolist(),
            [cust_state_codes[c] for c in order_customer.tolist()],
            [cust_zip[c] for c in order_customer.tolist()],
        ],
    )
    tables["order_items"] = write_table(
        out,
        "order_items",
        [
            (line_order + 1).tolist(),
            line_no.tolist(),
            (line_offer + 1).tolist(),
            (offer_seller[line_offer] + 1).tolist(),
            line_qty.tolist(),
            line_price.tolist(),
            [created_text[o] for o in line_order.tolist()],
        ],
    )

    shipped_mask = np.isin(status, [2, 3, 5])
    ship_orders = np.nonzero(shipped_mask)[0]
    room = end - 1 - created[ship_orders]
    shipped_at = created[ship_orders] + np.minimum(
        rng.integers(DAY // 5, DAY * 3 // 2, size=len(ship_orders)), room // 2
    )
    delivered_mask = np.isin(status[ship_orders], [3, 5])
    transit = rng.integers(DAY, 5 * DAY, size=len(ship_orders))
    delivered_at = shipped_at + np.minimum(transit, (end - 1 - shipped_at) // 2)
    carrier_p = np.array([c[2] for c in CARRIERS])
    carrier_pick = rng.choice(len(CARRIERS), size=len(ship_orders), p=carrier_p / carrier_p.sum())
    shipped_text = stamp(shipped_at)
    delivered_text = stamp(delivered_at)
    tables["shipments"] = write_table(
        out,
        "shipments",
        [
            (ship_orders + 1).tolist(),
            [CARRIERS[c][0] for c in carrier_pick.tolist()],
            [
                f"{CARRIERS[c][1]}{o + 1:012d}"
                for c, o in zip(carrier_pick.tolist(), ship_orders.tolist(), strict=True)
            ],
            shipped_text,
            [
                d if keep_d else ""
                for d, keep_d in zip(delivered_text, delivered_mask.tolist(), strict=True)
            ],
        ],
    )

    delivered_time = np.full(n_orders, -1, dtype=np.int64)
    delivered_time[ship_orders[delivered_mask]] = delivered_at[delivered_mask]
    review_rows = []
    seen = set()
    eligible = np.nonzero((status[line_order] == 3) & (rng.random(len(line_order)) < 0.18))[0]
    review_delay = rng.integers(DAY, 20 * DAY, size=len(eligible))
    ratings_pick = rng.choice(5, size=len(eligible), p=[0.06, 0.05, 0.10, 0.24, 0.55]) + 1
    title_pick = rng.integers(0, 100, size=len(eligible))
    for idx, delay, rating, tpick in zip(
        eligible.tolist(),
        review_delay.tolist(),
        ratings_pick.tolist(),
        title_pick.tolist(),
        strict=True,
    ):
        o = int(line_order[idx])
        when = int(delivered_time[o]) + delay
        if when >= end:
            continue
        product = int(product_of_offer[line_offer[idx]])
        customer = int(order_customer[o])
        if (product, customer) in seen:
            continue
        seen.add((product, customer))
        options = REVIEW_TITLES[rating]
        review_rows.append((when, product + 1, customer + 1, rating, options[tpick % len(options)]))
    review_rows.sort()
    tables["reviews"] = write_table(
        out,
        "reviews",
        [
            range(1, len(review_rows) + 1),
            [r[1] for r in review_rows],
            [r[2] for r in review_rows],
            [r[3] for r in review_rows],
            [r[4] for r in review_rows],
            stamp([r[0] for r in review_rows]),
        ],
    )

    cart_customers = np.sort(
        rng.choice(n["customers"], size=max(10, n["customers"] * 8 // 100), replace=False)
    )
    cart_lines = rng.integers(1, 5, size=len(cart_customers))
    cart_offer = pick_offers(int(cart_lines.sum()))
    cart_owner = np.repeat(cart_customers, cart_lines)
    sort = np.lexsort((cart_offer, cart_owner))
    cart_owner = cart_owner[sort]
    cart_offer = cart_offer[sort]
    keep = np.ones(len(cart_owner), dtype=bool)
    keep[1:] = (cart_owner[1:] != cart_owner[:-1]) | (cart_offer[1:] != cart_offer[:-1])
    cart_owner = cart_owner[keep]
    cart_offer = cart_offer[keep]
    cart_qty = rng.integers(1, 3, size=len(cart_owner))
    cart_added = end - rng.integers(60, 14 * DAY, size=len(cart_owner))
    tables["cart_items"] = write_table(
        out,
        "cart_items",
        [
            (cart_owner + 1).tolist(),
            (cart_offer + 1).tolist(),
            cart_qty.tolist(),
            stamp(cart_added),
        ],
    )

    manifest = {"params": params_for(cfg, low_stock), "tables": tables}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest
