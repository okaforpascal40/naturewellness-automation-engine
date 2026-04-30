-- Seed data for phytochemical_sources
-- Covers the full CTD phytochemical whitelist (app/api/ctd_api.py).
-- ON CONFLICT clause makes this safe to re-run.

INSERT INTO phytochemical_sources (phytochemical_name, fruit_vegetables, primary_sources, chemical_class) VALUES

-- ── Flavonoids ──────────────────────────────────────────────────────────────
('Quercetin', ARRAY['Onion','Kale','Broccoli','Apple','Capers','Berries','Red Grape','Cherry'], ARRAY['Onion (Red)','Capers','Kale'], 'Flavonoid'),
('Kaempferol', ARRAY['Spinach','Kale','Broccoli','Tea','Strawberry','Bean','Endive'], ARRAY['Spinach','Kale','Broccoli'], 'Flavonoid'),
('Myricetin', ARRAY['Berry','Walnut','Onion','Tea','Red Wine','Tomato'], ARRAY['Berry','Walnut','Tea'], 'Flavonoid'),
('Apigenin', ARRAY['Parsley','Celery','Chamomile','Thyme','Oregano','Artichoke'], ARRAY['Parsley','Celery','Chamomile'], 'Flavonoid'),
('Luteolin', ARRAY['Celery','Parsley','Artichoke','Broccoli','Pepper','Carrot','Olive Oil'], ARRAY['Celery','Parsley','Artichoke'], 'Flavonoid'),
('Naringenin', ARRAY['Grapefruit','Orange','Tomato','Bergamot'], ARRAY['Grapefruit','Orange'], 'Flavonoid'),
('Hesperidin', ARRAY['Orange','Lemon','Lime','Grapefruit','Tangerine'], ARRAY['Orange','Tangerine','Lemon'], 'Flavonoid'),
('Hesperetin', ARRAY['Orange','Lemon','Lime','Grapefruit'], ARRAY['Orange','Lemon'], 'Flavonoid'),
('Rutin', ARRAY['Buckwheat','Apple','Citrus','Asparagus','Fig'], ARRAY['Buckwheat','Asparagus','Fig'], 'Flavonoid'),
('Fisetin', ARRAY['Strawberry','Apple','Persimmon','Onion','Cucumber','Grape'], ARRAY['Strawberry','Persimmon','Apple'], 'Flavonoid'),
('Genistein', ARRAY['Soybean','Edamame','Tofu','Tempeh','Lupin','Fava Bean'], ARRAY['Soybean','Edamame','Tempeh'], 'Isoflavone'),
('Daidzein', ARRAY['Soybean','Edamame','Tofu','Tempeh','Chickpea'], ARRAY['Soybean','Edamame','Tempeh'], 'Isoflavone'),
('Glycitein', ARRAY['Soybean','Edamame','Tofu','Tempeh'], ARRAY['Soybean','Edamame'], 'Isoflavone'),
('Catechin', ARRAY['Green Tea','Black Tea','Cocoa','Apple','Grape','Berry'], ARRAY['Green Tea','Cocoa','Apple'], 'Flavonoid'),
('Epicatechin', ARRAY['Cocoa','Dark Chocolate','Green Tea','Apple','Grape'], ARRAY['Cocoa','Dark Chocolate','Green Tea'], 'Flavonoid'),
('Epigallocatechin', ARRAY['Green Tea','White Tea','Apple','Grape'], ARRAY['Green Tea','White Tea'], 'Flavonoid'),
('Epigallocatechin gallate', ARRAY['Green Tea','White Tea','Matcha'], ARRAY['Green Tea','Matcha'], 'Flavonoid'),
('Cyanidin', ARRAY['Blackberry','Blueberry','Cherry','Red Cabbage','Black Bean','Red Grape'], ARRAY['Blackberry','Blueberry','Cherry'], 'Anthocyanin'),
('Delphinidin', ARRAY['Blueberry','Blackcurrant','Eggplant','Pomegranate','Concord Grape'], ARRAY['Blueberry','Blackcurrant','Eggplant'], 'Anthocyanin'),
('Malvidin', ARRAY['Red Grape','Red Wine','Blueberry','Bilberry'], ARRAY['Red Grape','Bilberry'], 'Anthocyanin'),
('Pelargonidin', ARRAY['Strawberry','Raspberry','Pomegranate','Red Radish','Kidney Bean'], ARRAY['Strawberry','Raspberry'], 'Anthocyanin'),
('Peonidin', ARRAY['Cranberry','Blueberry','Plum','Cherry','Sweet Potato (Purple)'], ARRAY['Cranberry','Plum','Sweet Potato (Purple)'], 'Anthocyanin'),
('Petunidin', ARRAY['Blueberry','Bilberry','Black Grape','Andean Berry'], ARRAY['Blueberry','Bilberry'], 'Anthocyanin'),

-- ── Polyphenols / Stilbenes / Phenolic acids ────────────────────────────────
('Resveratrol', ARRAY['Grape','Blueberry','Cranberry','Peanut','Mulberry','Pistachio'], ARRAY['Grape (Red)','Blueberry','Mulberry'], 'Stilbenoid'),
('Pterostilbene', ARRAY['Blueberry','Grape','Almond'], ARRAY['Blueberry','Grape'], 'Stilbenoid'),
('Curcumin', ARRAY['Turmeric'], ARRAY['Turmeric Root'], 'Curcuminoid'),
('Ellagic acid', ARRAY['Pomegranate','Raspberry','Strawberry','Blackberry','Walnut','Pecan'], ARRAY['Pomegranate','Raspberry','Walnut'], 'Polyphenol'),
('Gallic acid', ARRAY['Grape','Berry','Mango','Tea','Walnut','Sumac'], ARRAY['Grape','Berry','Tea'], 'Phenolic acid'),
('Caffeic acid', ARRAY['Coffee','Apple','Carrot','Pear','Blueberry','Olive'], ARRAY['Coffee','Apple','Olive'], 'Phenolic acid'),
('Chlorogenic acid', ARRAY['Coffee','Apple','Pear','Blueberry','Eggplant','Sunflower Seed'], ARRAY['Coffee','Apple','Eggplant'], 'Phenolic acid'),
('Ferulic acid', ARRAY['Brown Rice','Whole Wheat','Oat','Coffee','Eggplant','Tomato'], ARRAY['Brown Rice','Whole Wheat','Oat'], 'Phenolic acid'),
('Rosmarinic acid', ARRAY['Rosemary','Sage','Basil','Thyme','Oregano','Mint'], ARRAY['Rosemary','Sage','Basil'], 'Phenolic acid'),
('Tannic acid', ARRAY['Tea','Pomegranate','Grape','Berry','Persimmon','Walnut'], ARRAY['Tea','Pomegranate','Persimmon'], 'Polyphenol'),

-- ── Carotenoids ─────────────────────────────────────────────────────────────
('Beta-carotene', ARRAY['Carrot','Sweet Potato','Pumpkin','Spinach','Kale','Cantaloupe','Apricot'], ARRAY['Carrot','Sweet Potato','Pumpkin'], 'Carotenoid'),
('Alpha-carotene', ARRAY['Carrot','Pumpkin','Sweet Potato','Winter Squash','Tangerine'], ARRAY['Carrot','Pumpkin','Winter Squash'], 'Carotenoid'),
('Lycopene', ARRAY['Tomato','Watermelon','Pink Grapefruit','Papaya','Guava','Red Pepper'], ARRAY['Tomato (Cooked)','Watermelon','Guava'], 'Carotenoid'),
('Lutein', ARRAY['Kale','Spinach','Collards','Egg Yolk','Corn','Broccoli','Pea'], ARRAY['Kale','Spinach','Collards'], 'Carotenoid'),
('Zeaxanthin', ARRAY['Corn','Egg Yolk','Spinach','Kale','Orange Pepper','Saffron'], ARRAY['Corn','Orange Pepper','Saffron'], 'Carotenoid'),
('Astaxanthin', ARRAY['Salmon','Shrimp','Krill','Lobster','Microalgae'], ARRAY['Salmon','Shrimp','Microalgae'], 'Carotenoid'),
('Cryptoxanthin', ARRAY['Tangerine','Persimmon','Papaya','Red Pepper','Pumpkin','Orange'], ARRAY['Tangerine','Persimmon','Papaya'], 'Carotenoid'),
('Fucoxanthin', ARRAY['Wakame','Hijiki','Kombu','Brown Seaweed'], ARRAY['Wakame','Hijiki','Brown Seaweed'], 'Carotenoid'),

-- ── Terpenes / Terpenoids ───────────────────────────────────────────────────
('Limonene', ARRAY['Lemon','Orange','Lime','Grapefruit','Mandarin'], ARRAY['Lemon Peel','Orange Peel','Grapefruit Peel'], 'Terpene'),
('Linalool', ARRAY['Lavender','Coriander','Basil','Mint','Cinnamon'], ARRAY['Lavender','Coriander','Basil'], 'Terpene'),
('Carvacrol', ARRAY['Oregano','Thyme','Marjoram','Savory','Wild Bergamot'], ARRAY['Oregano','Thyme'], 'Terpenoid'),
('Thymol', ARRAY['Thyme','Oregano','Marjoram','Savory'], ARRAY['Thyme','Oregano'], 'Terpenoid'),
('Geraniol', ARRAY['Geranium','Rose','Lemongrass','Lemon','Coriander'], ARRAY['Geranium','Lemongrass','Rose'], 'Terpene'),
('Menthol', ARRAY['Peppermint','Spearmint','Other Mint Species'], ARRAY['Peppermint','Spearmint'], 'Terpenoid'),
('Carnosic acid', ARRAY['Rosemary','Sage'], ARRAY['Rosemary','Sage'], 'Terpenoid'),
('Carnosol', ARRAY['Rosemary','Sage'], ARRAY['Rosemary','Sage'], 'Terpenoid'),
('Ursolic acid', ARRAY['Apple Peel','Cranberry','Basil','Rosemary','Oregano','Thyme'], ARRAY['Apple Peel','Cranberry','Basil'], 'Terpenoid'),
('Oleanolic acid', ARRAY['Olive','Garlic','Apple Peel','Clove','Mistletoe'], ARRAY['Olive','Garlic','Apple Peel'], 'Terpenoid'),
('Betulinic acid', ARRAY['Birch Bark','Plum','Jujube','Rosemary'], ARRAY['Birch Bark','Plum'], 'Terpenoid'),

-- ── Organosulfur ────────────────────────────────────────────────────────────
('Allicin', ARRAY['Garlic','Leek','Onion'], ARRAY['Garlic (Crushed Fresh)'], 'Organosulfur'),
('Diallyl sulfide', ARRAY['Garlic','Onion','Leek','Shallot','Chive'], ARRAY['Garlic','Onion'], 'Organosulfur'),
('Diallyl disulfide', ARRAY['Garlic','Onion','Leek','Shallot'], ARRAY['Garlic','Onion'], 'Organosulfur'),
('Sulforaphane', ARRAY['Broccoli','Brussels Sprouts','Cabbage','Kale','Cauliflower','Bok Choy'], ARRAY['Broccoli Sprouts','Broccoli','Brussels Sprouts'], 'Isothiocyanate'),
('Indole-3-carbinol', ARRAY['Broccoli','Cabbage','Kale','Brussels Sprouts','Cauliflower'], ARRAY['Broccoli','Brussels Sprouts'], 'Indole'),
('Diindolylmethane', ARRAY['Broccoli','Brussels Sprouts','Cabbage','Kale','Cauliflower'], ARRAY['Broccoli','Brussels Sprouts'], 'Indole'),

-- ── Alkaloids / Xanthines ───────────────────────────────────────────────────
('Caffeine', ARRAY['Coffee','Tea','Cocoa','Yerba Mate','Guarana'], ARRAY['Coffee','Tea','Cocoa'], 'Alkaloid'),
('Theobromine', ARRAY['Cocoa','Dark Chocolate','Tea','Yerba Mate'], ARRAY['Cocoa','Dark Chocolate'], 'Alkaloid'),
('Theophylline', ARRAY['Tea','Cocoa'], ARRAY['Tea','Cocoa'], 'Alkaloid'),
('Capsaicin', ARRAY['Chili Pepper','Cayenne','Jalapeño','Habanero','Bell Pepper'], ARRAY['Chili Pepper','Cayenne','Habanero'], 'Alkaloid'),
('Piperine', ARRAY['Black Pepper','White Pepper','Long Pepper'], ARRAY['Black Pepper'], 'Alkaloid'),
('Berberine', ARRAY['Goldenseal','Barberry','Oregon Grape','Tree Turmeric'], ARRAY['Goldenseal','Barberry'], 'Alkaloid'),

-- ── Lignans ─────────────────────────────────────────────────────────────────
('Lignan', ARRAY['Flaxseed','Sesame Seed','Whole Grain','Berry','Cruciferous Vegetable'], ARRAY['Flaxseed','Sesame Seed'], 'Lignan'),
('Enterolactone', ARRAY['Flaxseed','Sesame Seed','Rye','Berry'], ARRAY['Flaxseed','Sesame Seed'], 'Lignan'),
('Enterodiol', ARRAY['Flaxseed','Sesame Seed','Rye','Whole Grain'], ARRAY['Flaxseed','Sesame Seed'], 'Lignan'),
('Secoisolariciresinol', ARRAY['Flaxseed','Sesame Seed','Sunflower Seed','Whole Grain'], ARRAY['Flaxseed','Sesame Seed'], 'Lignan'),

-- ── Other notable phytochemicals ────────────────────────────────────────────
('Silymarin', ARRAY['Milk Thistle','Artichoke'], ARRAY['Milk Thistle'], 'Flavonolignan'),
('Silibinin', ARRAY['Milk Thistle'], ARRAY['Milk Thistle'], 'Flavonolignan'),
('Hydroxytyrosol', ARRAY['Olive','Olive Oil','Olive Leaf'], ARRAY['Extra Virgin Olive Oil','Olive'], 'Phenolic'),
('Oleuropein', ARRAY['Olive','Olive Oil','Olive Leaf'], ARRAY['Olive Leaf','Extra Virgin Olive Oil'], 'Phenolic'),
('Phloretin', ARRAY['Apple','Apple Peel','Strawberry'], ARRAY['Apple Peel','Apple'], 'Dihydrochalcone'),
('Phlorizin', ARRAY['Apple','Apple Peel','Pear','Cherry'], ARRAY['Apple Peel','Apple'], 'Dihydrochalcone'),
('Xanthohumol', ARRAY['Hops','Beer'], ARRAY['Hops'], 'Flavonoid'),
('Honokiol', ARRAY['Magnolia Bark'], ARRAY['Magnolia Bark'], 'Lignan'),
('Magnolol', ARRAY['Magnolia Bark'], ARRAY['Magnolia Bark'], 'Lignan'),
('Withaferin A', ARRAY['Ashwagandha','Winter Cherry'], ARRAY['Ashwagandha'], 'Steroidal lactone'),
('Gingerol', ARRAY['Ginger'], ARRAY['Ginger Root (Fresh)'], 'Phenolic'),
('Shogaol', ARRAY['Ginger'], ARRAY['Ginger Root (Dried)'], 'Phenolic'),
('Zingerone', ARRAY['Ginger'], ARRAY['Ginger Root (Cooked)'], 'Phenolic'),
('Eugenol', ARRAY['Clove','Cinnamon','Nutmeg','Basil','Bay Leaf'], ARRAY['Clove','Cinnamon'], 'Phenolic'),
('Vanillin', ARRAY['Vanilla Bean'], ARRAY['Vanilla Bean'], 'Phenolic aldehyde'),
('Anethole', ARRAY['Anise','Fennel','Star Anise','Licorice'], ARRAY['Anise','Fennel','Star Anise'], 'Phenylpropanoid'),

-- ── Generic class entries (catches CTD class-pattern matches) ───────────────
('Anthocyanins', ARRAY['Blueberry','Blackberry','Strawberry','Raspberry','Black Rice','Elderberry','Red Cabbage','Eggplant'], ARRAY['Blueberry','Blackberry','Elderberry'], 'Flavonoid')

ON CONFLICT (phytochemical_name) DO UPDATE SET
    fruit_vegetables = EXCLUDED.fruit_vegetables,
    primary_sources = EXCLUDED.primary_sources,
    chemical_class = EXCLUDED.chemical_class;
