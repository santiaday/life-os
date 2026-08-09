-- 0046_possession_vocab_seed.sql
--
-- Seed the controlled vocabularies. Separate from 0045 so the vocab can be
-- extended by a later migration without touching the DDL, and so a fresh
-- database comes up with a usable tagger prompt (the vision call reads the
-- colour list straight out of dim_color -- an empty table means the tagger
-- has nothing legal to return).
--
-- Deliberately coarse. Thirty-odd colours is enough to filter a closet and
-- few enough that a human reviewer picks the same one twice. 'midnight',
-- 'ecru' and 'oatmeal' are exactly the drift this table exists to prevent --
-- they belong in `colorway`, which is free text and describes the item as
-- sold.

BEGIN;

INSERT INTO dim_color (name, family, hex, sort_order) VALUES
  ('black',       'neutral', '#111111', 10),
  ('charcoal',    'neutral', '#3a3a3c', 11),
  ('grey',        'neutral', '#8e8e93', 12),
  ('light grey',  'neutral', '#c7c7cc', 13),
  ('white',       'neutral', '#ffffff', 14),
  ('cream',       'neutral', '#f2e8d5', 15),
  ('beige',       'neutral', '#d8c3a5', 16),
  ('tan',         'neutral', '#b08d57', 17),
  ('brown',       'brown',   '#6f4e37', 20),
  ('chocolate',   'brown',   '#3d2b1f', 21),
  ('rust',        'brown',   '#b7410e', 22),
  ('olive',       'green',   '#6b6b3a', 30),
  ('army green',  'green',   '#4b5320', 31),
  ('green',       'green',   '#2e8b57', 32),
  ('forest green','green',   '#14432a', 33),
  ('mint',        'green',   '#a8e6cf', 34),
  ('teal',        'blue',    '#008080', 40),
  ('light blue',  'blue',    '#a5c8e1', 41),
  ('blue',        'blue',    '#2d68c4', 42),
  ('navy',        'blue',    '#1b264f', 43),
  ('indigo',      'blue',    '#3f3f74', 44),
  ('purple',      'purple',  '#6a4c93', 50),
  ('lavender',    'purple',  '#c3b1e1', 51),
  ('pink',        'red',     '#f4a6b8', 60),
  ('red',         'red',     '#c1272d', 61),
  ('burgundy',    'red',     '#5c1a2b', 62),
  ('orange',      'orange',  '#e8722c', 70),
  ('mustard',     'yellow',  '#d4a017', 80),
  ('yellow',      'yellow',  '#f2c94c', 81),
  ('gold',        'metal',   '#c9a227', 90),
  ('silver',      'metal',   '#c0c0c0', 91),
  ('multicolor',  'other',   NULL,      98),
  ('clear',       'other',   NULL,      99)
ON CONFLICT (name) DO NOTHING;

INSERT INTO dim_possession_subcategory (kind, name) VALUES
  -- tops
  ('top','crew tee'), ('top','v-neck tee'), ('top','long sleeve tee'),
  ('top','polo'), ('top','button-down shirt'), ('top','oxford shirt'),
  ('top','flannel shirt'), ('top','henley'), ('top','tank top'),
  ('top','sweatshirt'), ('top','hoodie'), ('top','crewneck sweater'),
  ('top','quarter zip'), ('top','base layer'), ('top','performance tee'),
  ('top','dress shirt'),
  -- bottoms
  ('bottom','jeans'), ('bottom','chinos'), ('bottom','dress trousers'),
  ('bottom','joggers'), ('bottom','sweatpants'), ('bottom','shorts'),
  ('bottom','athletic shorts'), ('bottom','swim trunks'),
  ('bottom','hiking pants'), ('bottom','leggings'), ('bottom','underwear'),
  ('bottom','socks'),
  -- outerwear
  ('outerwear','rain shell'), ('outerwear','wind shell'),
  ('outerwear','puffer jacket'), ('outerwear','down vest'),
  ('outerwear','fleece'), ('outerwear','denim jacket'),
  ('outerwear','bomber jacket'), ('outerwear','leather jacket'),
  ('outerwear','overcoat'), ('outerwear','blazer'), ('outerwear','parka'),
  ('outerwear','softshell'),
  -- footwear
  ('footwear','running shoe'), ('footwear','trainer'),
  ('footwear','sneaker'), ('footwear','dress shoe'), ('footwear','loafer'),
  ('footwear','boot'), ('footwear','hiking boot'), ('footwear','sandal'),
  ('footwear','slipper'), ('footwear','cleat'),
  -- headwear
  ('headwear','baseball cap'), ('headwear','beanie'),
  ('headwear','bucket hat'), ('headwear','sun hat'), ('headwear','helmet'),
  -- accessory
  ('accessory','belt'), ('accessory','watch'), ('accessory','sunglasses'),
  ('accessory','glasses'), ('accessory','scarf'), ('accessory','gloves'),
  ('accessory','tie'), ('accessory','jewelry'), ('accessory','wallet'),
  ('accessory','umbrella'),
  -- bags
  ('bag','backpack'), ('bag','daypack'), ('bag','duffel'),
  ('bag','carry-on suitcase'), ('bag','checked suitcase'),
  ('bag','packing cube'), ('bag','tote'), ('bag','sling'),
  ('bag','laptop sleeve'), ('bag','dopp kit'),
  -- tech
  ('tech','laptop'), ('tech','tablet'), ('tech','phone'),
  ('tech','headphones'), ('tech','earbuds'), ('tech','charger'),
  ('tech','cable'), ('tech','power bank'), ('tech','travel adapter'),
  ('tech','camera'), ('tech','e-reader'), ('tech','wearable'),
  ('tech','hard drive'), ('tech','mouse'), ('tech','keyboard'),
  -- toiletries
  ('toiletry','toothbrush'), ('toiletry','toothpaste'),
  ('toiletry','razor'), ('toiletry','deodorant'), ('toiletry','shampoo'),
  ('toiletry','body wash'), ('toiletry','skincare'),
  ('toiletry','hair product'), ('toiletry','sunscreen'),
  ('toiletry','fragrance'), ('toiletry','medication'),
  ('toiletry','nail clippers'), ('toiletry','contact lenses'),
  -- gear
  ('gear','water bottle'), ('gear','travel pillow'),
  ('gear','eye mask'), ('gear','earplugs'), ('gear','lifting belt'),
  ('gear','resistance band'), ('gear','jump rope'), ('gear','yoga mat'),
  ('gear','tent'), ('gear','sleeping bag'), ('gear','first aid kit'),
  ('gear','laundry bag'), ('gear','travel towel'),
  -- other
  ('other','document'), ('other','book'), ('other','misc')
ON CONFLICT (kind, name) DO NOTHING;

COMMIT;
