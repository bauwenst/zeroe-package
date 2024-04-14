from zeroe.attacks.simple_attacks import simple_perturb

sentence = "I like apples very much."
print("Full Swap:", simple_perturb(sentence, 'full-swap', perturbation_level=0.3))
print("Inner Swap:", simple_perturb(sentence, 'inner-swap', perturbation_level=0.3))
print("Intruders:", simple_perturb(sentence, 'intrude', perturbation_level=1.0))
print("Disemvoweling:", simple_perturb(sentence, 'disemvowel', perturbation_level=0.3))
print("Truncating:", simple_perturb(sentence, 'truncate', perturbation_level=0.3))
print("Key Typo:", simple_perturb(sentence, 'keyboard-typo', perturbation_level=0.3))
print("Natural Typo:", simple_perturb(sentence, 'natural-typo', perturbation_level=0.3))
print("Segmentation:", simple_perturb(sentence, 'segment', perturbation_level=0.5))
