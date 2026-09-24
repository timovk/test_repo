"""Name pools for FICTIONAL candidates.

Common Dutch given names and surnames (including *tussenvoegsels* such as ``van``, ``de``,
``van der``, ``ter``) plus regional (Frisian/Groningen, Brabant/Limburg) and migrant-heritage
pools reflecting the diversity of Dutch society.  Candidates are random combinations, so they
are fictional people; surnames strongly associated with prominent Dutch politicians are left
out and :data:`BLOCKED_FULL_NAMES` rejects a few well-known combinations outright.
"""

from __future__ import annotations

# fmt: off
GIVEN_NAMES_MALE: tuple[str, ...] = (
    "Jan", "Pieter", "Kees", "Henk", "Bram", "Daan", "Thijs", "Sander", "Joost", "Maarten",
    "Wouter", "Jeroen", "Bas", "Niels", "Tim", "Lars", "Koen", "Stijn", "Ruben", "Jasper",
    "Martijn", "Erik", "Frank", "Marco", "Dennis", "Hans", "Gerrit", "Willem", "Johan", "Arjen",
    "Sjoerd", "Tjeerd", "Hidde", "Joris", "Mees", "Sem", "Luuk", "Floris", "Hugo", "Olivier",
    "Ruud", "Remco", "Mark", "Paul", "Peter", "Rik", "Jurgen", "Vincent", "Michiel", "Gijs",
    "Teun", "Jelle", "Wessel", "Jorrit", "Hendrik", "Cornelis", "Dirk", "Klaas", "Freek", "Ton",
    "Gert", "Harm", "Evert", "Roelof", "Jacob", "Lucas", "Stefan", "Bart", "Robin", "Twan",
    "Guus", "Job", "Siebe", "Wim", "Rens", "Tijmen", "Ivo", "Arnoud", "Coen", "Menno",
    "Ronald", "Edwin", "Patrick", "Richard", "Robert", "Laurens", "Christiaan", "Diederik", "Egbert", "Frits",
    "Geert-Jan", "Herman", "Jaap", "Joep", "Lodewijk", "Matthijs", "Nick", "Otto", "Quinten", "Reinier",
    "Stan", "Thomas", "Victor", "Wiebe", "Yorick", "Anton", "Ben", "Chris", "Douwe", "Emiel",
)

GIVEN_NAMES_FEMALE: tuple[str, ...] = (
    "Anne", "Anouk", "Sanne", "Lotte", "Femke", "Esther", "Marieke", "Ingrid", "Linda", "Monique",
    "Saskia", "Petra", "Karin", "Wilma", "Els", "Ria", "Anja", "Janneke", "Ilse", "Marloes",
    "Iris", "Eva", "Fleur", "Julia", "Sophie", "Emma", "Noor", "Lieke", "Roos", "Merel",
    "Maud", "Floor", "Nienke", "Hanneke", "Joke", "Tineke", "Gerda", "Hilde", "Annemiek", "Brenda",
    "Carla", "Daphne", "Evelien", "Gonnie", "Heleen", "Inge", "Jolanda", "Kim", "Loes", "Mirjam",
    "Nathalie", "Paulien", "Renate", "Sylvia", "Tessa", "Vera", "Wendy", "Yvonne", "Astrid", "Bianca",
    "Charlotte", "Dieuwke", "Elise", "Freya", "Geertje", "Hester", "Irene", "Jet", "Karlijn", "Lisanne",
    "Mariska", "Nicole", "Olga", "Pien", "Rianne", "Suzanne", "Trijntje", "Ursula", "Wietske", "Yara",
    "Annelies", "Berber", "Cato", "Doutzen", "Eline", "Frederique", "Gea", "Hennie", "Isabel", "Jildou",
    "Kirsten", "Liesbeth", "Margriet", "Nynke", "Oda", "Priscilla", "Rixt", "Sietske", "Tamara", "Willemijn",
)

GIVEN_NAMES_MALE_HERITAGE: tuple[str, ...] = (
    "Mohamed", "Youssef", "Mehmet", "Ahmed", "Rachid", "Karim", "Emre", "Hakan", "Mustafa", "Said",
    "Bilal", "Omar", "Anil", "Ravi", "Rajesh", "Dewi", "Kofi", "Samuel", "Mauricio", "Tomasz",
)

GIVEN_NAMES_FEMALE_HERITAGE: tuple[str, ...] = (
    "Fatima", "Samira", "Nadia", "Yasmina", "Aylin", "Elif", "Sevda", "Khadija", "Naima", "Hatice",
    "Zeynep", "Latifa", "Priya", "Sharmila", "Anjali", "Esmeralda", "Chantal", "Agnieszka", "Maryam", "Leila",
)

SURNAMES_COMMON: tuple[str, ...] = (
    "de Jong", "Jansen", "de Vries", "van den Berg", "van Dijk", "Bakker", "Visser", "Smit", "Meijer",
    "de Boer", "Mulder", "de Groot", "Vos", "Peters", "van Leeuwen", "Dekker", "Brouwer", "de Wit",
    "de Graaf", "van der Meer", "van der Linden", "Jacobs", "de Haan", "Vermeulen", "van den Heuvel",
    "van der Veen", "van den Broek", "de Bruijn", "de Bruin", "van der Heijden", "Schouten", "van Beek",
    "Willems", "van Vliet", "Koster", "van Dam", "van der Wal", "Prins", "Blom", "Huisman",
    "Kuipers", "van Veen", "Post", "Kuiper", "Kramer", "van den Brink", "Scholten", "van Wijk",
    "Vink", "de Ruiter", "Groen", "Gerritsen", "Jonker", "van Loon", "Boer", "van der Velde",
    "Willemsen", "de Lange", "de Vos", "Bosch", "van Dongen", "Schipper", "de Koning", "van der Laan",
    "Koning", "van der Velden", "Driessen", "van Doorn", "Evers", "van Rijn", "Ruiter", "Wolters",
    "Kok", "Bos", "Hoogendoorn", "Verbeek", "van Os", "Timmer", "Brinkman", "Molenaar", "Kort",
    "van Kessel", "Zwart", "Hoekman", "van Es", "Stam", "Veenendaal", "ter Horst", "ter Beek",
    "van 't Hof", "van de Kamp", "Verhoef", "Pronk", "Staal", "Schuurman", "Hofman", "Klein",
    "van Wijngaarden", "van der Steen", "Roos", "Lammers", "Vonk", "Wijnands", "Hendriksen", "Kroon",
    "van Rooij", "Nijland", "de Kok", "van Gils", "Buitenhuis", "Vriend", "van Kampen", "Steenbergen",
    "Mol", "Kamphuis", "van Rossum", "Oosterhuis", "van der Ploeg", "Westerhof", "Ten Brink", "Bouwman",
    "Dijkman", "Groenewegen", "van Egmond", "Aalbers", "Baas", "van Ommen", "Keizer", "van Lieshout",
    "van Beusekom", "Hoek", "Sterk", "Tuinman", "van der Horst", "Rademaker", "Everts", "van Zanten",
)

SURNAMES_NORTH: tuple[str, ...] = (
    "Dijkstra", "Postma", "Veenstra", "Bouma", "Haaksma", "Sikkema", "Hiemstra", "Kooistra", "Jansma",
    "Hofstra", "Faber", "Bosma", "Wijnstra", "Algra", "de Haan", "Kuipers", "Boersma", "Mulder",
    "Hoekstra", "Tuinstra", "Wiegersma", "Terpstra", "Holtrop", "Nijboer", "Oosting", "Wolthuis",
    "Hazenberg", "Brouwer", "Zijlma", "Sijbesma", "Pool", "Venema", "Smedes", "Bakker", "Huizinga",
)

SURNAMES_SOUTH: tuple[str, ...] = (
    "Janssen", "Peeters", "Hendriks", "Smeets", "van de Ven", "Verhoeven", "Maas", "Martens",
    "Claessens", "Coenen", "Houben", "Schreurs", "Wouters", "Gielen", "Vossen", "Frissen", "Kurvers",
    "Rutten", "van den Boom", "Verstappen", "Lemmens", "Hermens", "Brouns", "Pijnenburg", "van Hout",
    "Verbruggen", "Aarts", "van Dommelen", "Swinkels", "van der Aa", "Raaijmakers", "Beerens",
    "Leenders", "Wijnen", "Crijns", "Bemelmans", "Hoebers", "Vranken", "Mertens", "van Gestel",
)

SURNAMES_HERITAGE: tuple[str, ...] = (
    "El Amrani", "Bouzid", "Yılmaz", "Kaya", "Demir", "Şahin", "Çelik", "Öztürk", "Aydın", "Ramdin",
    "Sewnarain", "Bakkali", "El Idrissi", "Ait Taleb", "Doğan", "Koç", "Kartosen", "Soerjadi", "Mensah",
    "Pinas", "Wijngaarde", "Nowak", "Kowalski", "Haddou", "Ouali", "Jagesar", "Baldewsingh", "Tahiri",
)

# fmt: on

#: Full names that must never be generated (well-known Dutch public figures).
BLOCKED_FULL_NAMES: frozenset[str] = frozenset(
    {
        "mark rutte",
        "geert wilders",
        "frans timmermans",
        "dick schoof",
        "pieter omtzigt",
        "caroline van der plas",
        "rob jetten",
        "sigrid kaag",
        "wopke hoekstra",
        "jesse klaver",
        "lilian marijnissen",
        "thierry baudet",
        "henri bontenbal",
        "hugo de jonge",
        "mona keijzer",
        "jan peter balkenende",
        "wim kok",
        "ruud lubbers",
        "joop den uyl",
        "dries van agt",
        "femke halsema",
        "ahmed aboutaleb",
        "klaas dijkhoff",
        "martin bosma",
        "fleur agema",
        "marjolein faber",
        "dilan yesilgoz",
        "sophie hermans",
        "eelco heinen",
        "ruben brekelmans",
        "gijs tuinman",
        "wouter bos",
        "job cohen",
        "lodewijk asscher",
        "diederik samsom",
        "hans wiegel",
        "pim fortuyn",
        "frits bolkestein",
        "gerrit zalm",
        "paul de krom",
        "stef blok",
        "halbe zijlstra",
        "ronald plasterk",
        "jeroen dijsselbloem",
        "eddy van hijum",
        "chris stoffer",
        "mirjam bikker",
        "kees van der staaij",
        "gert-jan segers",
        "sybrand buma",
        "esther ouwehand",
        "marianne thieme",
        "jan marijnissen",
        "emile roemer",
        "alexander pechtold",
        "thom de graaf",
        "hans van mierlo",
        "els borst",
        "jan terlouw",
    }
)
