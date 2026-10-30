# Services IA et confidentialité

[简体中文](ai-services.md) | [English](ai-services.en.md) | [Français](ai-services.fr.md)

[Retour à l’accueil](../README.fr.md) · [Documentation](README.fr.md)

## Outils IA locaux et assistant de retouche

| Fonction | Utilisation |
| --- | --- |
| Outils IA locaux : super-résolution, réduction du bruit, détourage, retouche de portrait, etc. | Préparez les modèles nécessaires dans « Paramètres → Outils et modèles ». Les photos sont traitées sur votre ordinateur. Le moteur GPU facultatif se gère dans « Paramètres → Moteurs d’exécution » |
| Assistant de retouche IA | Configurez une connexion et un modèle dans « Paramètres → Services IA ». Le texte, les paramètres et le contenu des photos que vous avez choisis sont envoyés à ce service, qui renvoie des réponses, des propositions ou des résultats d’appels d’outils |

Le téléchargement des modèles et la connexion aux services se gèrent séparément. Les outils locaux fonctionnent hors ligne une fois leurs modèles et leur moteur prêts. L’assistant en ligne doit pouvoir joindre le service choisi.

## Configurer la connexion à un service

1. Ouvrez « Paramètres → Services IA », puis cliquez sur « Ajouter ».
2. Renseignez le nom de la connexion, l’adresse de base indiquée par le fournisseur et la clé d’API nécessaire. L’adresse de base peut être la racine du service ou l’adresse complète de l’interface Chat Completions ou Responses.
3. Dans « Options de connexion », choisissez le protocole API et le mode d’authentification demandés par le service. Si vous avez besoin d’un proxy ou d’en-têtes supplémentaires, renseignez aussi ces réglages.
4. Cliquez sur « Vérifier et charger les modèles », puis ajoutez des modèles depuis la liste renvoyée. Si le service ne renvoie pas de liste, utilisez « Ajouter un modèle manuellement » et saisissez l’ID exact du modèle.
5. Développez « Réglages du modèle » et configurez, selon ce que le modèle prend réellement en charge, la réception d’images, l’appel d’outils, la réponse en flux et la limite de sortie.
6. Lancez le test du modèle pour vérifier qu’il répond. Vous pouvez définir par défaut le modèle que vous utilisez le plus, puis revenir au panneau de l’assistant pour commencer.

Le test du modèle envoie un court message de test. L’usage de l’API, les frais et les quotas, pour les tests comme pour les discussions courantes, sont comptés sur le compte du service choisi. Une tâche à plusieurs étapes de l’Agent peut comporter plusieurs requêtes.

## Choisir les capacités du modèle selon l’usage

| Cas d’usage | Capacités à configurer |
| --- | --- |
| Discussion et conseils de photographie | Un modèle de texte utilisable. S’il n’accepte pas les images, choisissez « Ne pas envoyer la photo » |
| Analyse visuelle de la photo en cours, d’une image de référence ou d’une proposition | Un modèle qui accepte les images, avec « Réception d’images » activée dans « Réglages du modèle » |
| Appel des outils de retouche par l’Agent | Un modèle qui prend en charge l’appel d’outils, avec « Prise en charge des appels d’outils » activée dans « Réglages du modèle » |
| Observation détaillée d’une zone | « Réception d’images » activée, puis les options d’image, comme « Grand côté effectif », réglées selon les capacités du modèle |

Les capacités du modèle dépendent de ce que le service prend réellement en charge. La réception d’images, l’appel d’outils et la réponse en flux se configurent séparément.

## Utiliser l’assistant

| Mode | Tâches adaptées |
| --- | --- |
| Discussion | Échanger sur la photographie, l’image et les idées de traitement |
| Planification | Examiner la photo en cours et l’état de la retouche, discuter d’un plan de traitement, sans modifier la photo |
| Réglage | Générer et appliquer, selon votre demande, les réglages de couleur ou de portrait adaptés |
| Agent | Enchaîner plusieurs appels d’outils autour d’un objectif, traiter la photo et vérifier le résultat |

Les retouches que l’assistant applique avec succès sont écrites dans l’historique de la photo : vous pouvez les annuler, les rétablir ou continuer à les modifier à la main. Pour comparer deux propositions, demandez des variantes A/B. Elles servent d’abord à la prévisualisation et à la comparaison, et ne sont écrites dans la retouche en cours qu’après un clic sur « Adopter ».

Vous pouvez ajouter une image de référence pour exprimer votre objectif, ou demander d’enregistrer un style nommé ou une préférence durable. L’enregistrement d’un style ou d’une préférence doit être confirmé dans une carte de confirmation.

## Options d’envoi des photos

Avant d’envoyer un message, vous pouvez choisir sous le champ de saisie comment les photos sont envoyées. Le haut du panneau de l’assistant indique ce qui peut être envoyé pour la requête en cours.

| Option | Contenu photo envoyé |
| --- | --- |
| Vue actuelle | Par défaut, le premier message joint un aperçu de la photo en cours, retouches comprises, avec un grand côté de 1536 pixels au maximum |
| Originale | Le premier message joint l’image sans les retouches, avec un grand côté de 2048 pixels au maximum |
| Ne pas envoyer la photo | La requête ne joint aucune photo ; les images de référence et les photos renvoyées par les observations d’outils ne sont pas envoyées non plus |

Une image plus petite que ces dimensions est envoyée à sa taille d’origine, sans agrandissement. Si la réception d’images n’est pas activée pour le modèle, aucune photo n’est envoyée non plus.

Quand l’envoi des photos est activé, l’assistant peut aussi consulter, selon ses besoins, des aperçus de l’image d’origine, de l’image avant le début de la tâche et de l’image actuelle, ainsi que des détails à haute résolution si les réglages du modèle l’autorisent. Une image de référence ajoutée est jointe ou non selon l’option d’envoi des photos et la capacité d’image du modèle. Toutes les photos envoyées sont des JPEG réencodés, sans les informations de prise de vue du fichier d’origine.

## Texte, paramètres et analyse de la photo

En plus des photos, l’assistant peut envoyer ce qui suit, selon les options de contexte :

- Le texte que vous saisissez, ainsi que l’historique de discussion choisi, les consignes fixes et le résumé de la discussion.
- Les paramètres de réglage actuels, le résumé du flux de travail et les résultats d’outils utiles à la tâche en cours.
- Une analyse de la photo calculée sur votre ordinateur, par exemple la luminosité, les dominantes de couleur et les visages. Ces informations sont envoyées sous forme de texte ou de valeurs.
- Le style nommé choisi, les préférences durables confirmées et l’analyse correspondante d’une image de référence.

Avec « Ne pas envoyer la photo », le texte, les paramètres et les analyses autorisés peuvent toujours faire partie de la requête. Vous pouvez ajuster ce qui est envoyé dans les options de contexte de l’assistant.

## Données locales et données côté service

Les clés d’API, les en-têtes supplémentaires et les identifiants du proxy sont chiffrés avec Windows DPAPI puis enregistrés sur votre ordinateur. Les discussions, les pièces jointes d’aperçu des photos, les styles nommés et les préférences sont aussi enregistrés localement ; le contenu choisi comme contexte d’une requête est envoyé au service choisi.

Vous pouvez supprimer les discussions inutiles ou effacer les pièces jointes photo d’une discussion. Effacer les pièces jointes conserve la photo d’origine, les messages et l’historique de retouche de la photo ; pour revoir la photo ensuite, vous pouvez joindre de nouveau la photo en cours.

Les règles de conservation et d’utilisation des données côté service dépendent du fournisseur choisi. Consultez les réglages et la politique de confidentialité de votre compte pour choisir le service et l’étendue d’envoi qui vous conviennent.

## Problèmes de connexion courants

| Problème | Que faire |
| --- | --- |
| La connexion ne se vérifie pas, ou une erreur d’authentification s’affiche | Vérifiez l’adresse de base, le protocole API, le mode d’authentification, la clé et les droits du compte ; avec un proxy, vérifiez son adresse |
| Le service ne renvoie pas de liste de modèles | Ajoutez le modèle manuellement, avec l’ID donné par le fournisseur |
| Un message indique que le modèle n’accepte pas les images | Choisissez un modèle qui accepte les images et activez « Réception d’images », ou passez l’option photo sur « Ne pas envoyer la photo » |
| L’Agent ne peut que discuter | Choisissez un modèle qui prend en charge l’appel d’outils et activez « Prise en charge des appels d’outils » dans les réglages du modèle |
| La requête expire ou la réponse en flux échoue | Vérifiez le réseau et l’état du service, ajustez le délai de requête selon les capacités du service ou désactivez la réponse en flux, puis réessayez |
| Un message indique que le contexte dépasse le budget | Utilisez « Réduire le contexte », réduisez ce qui est envoyé ou ajustez la fenêtre de contexte selon les capacités réelles du modèle |
| Un message indique que l’espace des pièces jointes est insuffisant | Supprimez les discussions inutiles ou effacez les pièces jointes photo, puis joignez de nouveau les photos nécessaires |

Pour signaler un problème, indiquez dans [GitHub Issues](https://github.com/KpTanl/OpenPictureStudio/issues) la version de l’application, le message d’erreur et les étapes pour le reproduire. Avant de publier une pièce jointe, retirez-en les clés d’API, les identifiants de proxy et les photos privées.
