# canvas rev 7; lines marked "# user" were set by the user
root = Column([title, cover, controls, player, more, viz])
title = Text("Pick a playlist", "h2")
cover = Image("https://cdn.example.com/covers/lofi-beats.jpg", "Album cover")
controls = Row([genre, play, skip])
genre = Select("Genre", ["Jazz", "Lo-fi", "Classical"], "Jazz")
genre.value = "Lo-fi"  # user
play = Button("Play", "primary", disabled=true)
skip = Button("Skip")
player = Audio("https://cdn.example.com/audio/rainy-day.mp3", "Rainy Day - Lo-fi Beats")  # user: playing at 0:42
more = Link("Open on Bandcamp", "https://milesdavis.bandcamp.com/album/kind-of-blue")
viz = Html(..., 60)  # 64 chars; canvas_get full=true shows the body
# events since your last read (1):
#   e17 genre change value="Lo-fi" by="alice" 12s ago
