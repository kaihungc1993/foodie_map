I want you to build a GitHub webpage to show a map with a collection of restaurants and other metadata info.

Source:
I want you to crawl all IG posts from born2eat_taiwan account. He posted 1 restaurant per post and include information that I want to extracted. Here's the info schema

* Restaurant Name
* Recommended by
* Price Range
* # of visited
* Newest rating
* Category (壽司/居酒屋/法餐/finedining/...) multi-selection
* address (with a button to open in Google Map)
* Personal note

Lists
* post content (expandable)
* IG link

Note that you need to group by restaurant. There might be X posts talking about the same one. The # of visted and newest rating should be based on the newest post (you need to do some aggregation)


I want to be able to show these restaurants on a Google map in the webpage.
With filters below:
category (selection filter)
rating (slider filter)

To pin it on the map, you'll for sure need the address

Personal note is to allow me to add / edit and save notes if I go visit there. 


Renew:
I want the webpage to renew (crawl latest post and add new ones to map or update existing one). So plan that accordingly. Do set up some regular jobs on GitHub to achieve that. 
