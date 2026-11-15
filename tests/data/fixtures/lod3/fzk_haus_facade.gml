<?xml version="1.0" encoding="UTF-8"?>
<!--
  A hand-made LoD3 facade export for the building of the FZK-Haus LoD2 model
  (input/citygml/FZK-Haus/FZK-Haus-LoD2-KIT-IAI-KHH-B36-V1.gml, same gml:id), the input of
  tests/integration/test_lod3_facades.py. Not the result of a real reconstruction.

  It carries the three defects pykci.enrich_lod3_facades repairs (LOD3_FACADE_INTEGRATION.md, 2):
    (a) the shell solid stands after bldg:boundedBy, where only lod3Solid is allowed
    (b) surfaces with openings and wall holes are tagged lod2MultiSurface, the shell lod2Solid
    (c) the CityModel envelope is a small box far away from the geometry

  First building (in the graph): the south wall of the house (N 5439083, E 457842..457854,
  height 111.8..115.43) with one window and one door, each as a hole of the wall polygon and
  as a bldg:opening with its own polygon; and the south half of the roof without openings.
  Its thematic values differ from the LoD2 model on purpose: they must never reach the graph.
  Second building: not in the graph, so it is reported and nothing of it is written. It has
  no opening and no hole (a LoD2 building), so its solid is moved in front of bldg:boundedBy
  instead of being renamed.
-->
<core:CityModel xmlns:core="http://www.opengis.net/citygml/2.0"
                xmlns:bldg="http://www.opengis.net/citygml/building/2.0"
                xmlns:gml="http://www.opengis.net/gml"
                xmlns:xlink="http://www.w3.org/1999/xlink">
  <gml:boundedBy>
    <gml:Envelope srsName="urn:adv:crs:ETRS89_UTM32*DE_DHHN92_NH" srsDimension="3">
      <gml:lowerCorner>457000 5439000 0</gml:lowerCorner>
      <gml:upperCorner>457003 5439014 3</gml:upperCorner>
    </gml:Envelope>
  </gml:boundedBy>
  <core:cityObjectMember>
    <bldg:Building gml:id="UUID_d281adfc-4901-0f52-540b-4cc1a9325f82">
      <bldg:function>9999</bldg:function>
      <bldg:measuredHeight uom="m">9.99</bldg:measuredHeight>
      <bldg:storeysAboveGround>7</bldg:storeysAboveGround>
      <bldg:boundedBy>
        <bldg:WallSurface gml:id="FZK_L3_WALL_SOUTH">
          <bldg:lod2MultiSurface>
            <gml:MultiSurface gml:id="FZK_L3_WALL_SOUTH_MS">
              <gml:surfaceMember>
                <gml:Polygon gml:id="FZK_L3_WALL_SOUTH_POLY">
                  <gml:exterior>
                    <gml:LinearRing gml:id="FZK_L3_WALL_SOUTH_RING">
                      <gml:posList srsDimension="3">457854 5439083 115.430940107676 457842 5439083 115.430940107676 457842 5439083 111.8 457854 5439083 111.8 457854 5439083 115.430940107676</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                  <gml:interior>
                    <gml:LinearRing gml:id="FZK_L3_WALL_SOUTH_HOLE_WINDOW">
                      <gml:posList srsDimension="3">457844 5439083 112.8 457844 5439083 114.0 457846 5439083 114.0 457846 5439083 112.8 457844 5439083 112.8</gml:posList>
                    </gml:LinearRing>
                  </gml:interior>
                  <gml:interior>
                    <gml:LinearRing gml:id="FZK_L3_WALL_SOUTH_HOLE_DOOR">
                      <gml:posList srsDimension="3">457849 5439083 111.9 457849 5439083 113.9 457850 5439083 113.9 457850 5439083 111.9 457849 5439083 111.9</gml:posList>
                    </gml:LinearRing>
                  </gml:interior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
          <bldg:opening>
            <bldg:Window gml:id="FZK_L3_WINDOW">
              <bldg:lod3MultiSurface>
                <gml:MultiSurface gml:id="FZK_L3_WINDOW_MS">
                  <gml:surfaceMember>
                    <gml:Polygon gml:id="FZK_L3_WINDOW_POLY">
                      <gml:exterior>
                        <gml:LinearRing gml:id="FZK_L3_WINDOW_RING">
                          <gml:posList srsDimension="3">457846 5439083 112.8 457846 5439083 114.0 457844 5439083 114.0 457844 5439083 112.8 457846 5439083 112.8</gml:posList>
                        </gml:LinearRing>
                      </gml:exterior>
                    </gml:Polygon>
                  </gml:surfaceMember>
                </gml:MultiSurface>
              </bldg:lod3MultiSurface>
            </bldg:Window>
          </bldg:opening>
          <bldg:opening>
            <bldg:Door gml:id="FZK_L3_DOOR">
              <bldg:lod3MultiSurface>
                <gml:MultiSurface gml:id="FZK_L3_DOOR_MS">
                  <gml:surfaceMember>
                    <gml:Polygon gml:id="FZK_L3_DOOR_POLY">
                      <gml:exterior>
                        <gml:LinearRing gml:id="FZK_L3_DOOR_RING">
                          <gml:posList srsDimension="3">457850 5439083 111.9 457850 5439083 113.9 457849 5439083 113.9 457849 5439083 111.9 457850 5439083 111.9</gml:posList>
                        </gml:LinearRing>
                      </gml:exterior>
                    </gml:Polygon>
                  </gml:surfaceMember>
                </gml:MultiSurface>
              </bldg:lod3MultiSurface>
            </bldg:Door>
          </bldg:opening>
        </bldg:WallSurface>
      </bldg:boundedBy>
      <bldg:boundedBy>
        <bldg:RoofSurface gml:id="FZK_L3_ROOF_SOUTH">
          <bldg:lod2MultiSurface>
            <gml:MultiSurface gml:id="FZK_L3_ROOF_SOUTH_MS">
              <gml:surfaceMember>
                <gml:Polygon gml:id="FZK_L3_ROOF_SOUTH_POLY">
                  <gml:exterior>
                    <gml:LinearRing gml:id="FZK_L3_ROOF_SOUTH_RING">
                      <gml:posList srsDimension="3">457854 5439083 115.430940107676 457854 5439088 118.317691453624 457842 5439088 118.317691453624 457842 5439083 115.430940107676 457854 5439083 115.430940107676</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
        </bldg:RoofSurface>
      </bldg:boundedBy>
      <bldg:lod2Solid>
        <gml:Solid gml:id="FZK_L3_SOLID">
          <gml:exterior>
            <gml:CompositeSurface gml:id="FZK_L3_SHELL">
              <gml:surfaceMember xlink:href="#FZK_L3_WALL_SOUTH_POLY"/>
              <gml:surfaceMember xlink:href="#FZK_L3_ROOF_SOUTH_POLY"/>
            </gml:CompositeSurface>
          </gml:exterior>
        </gml:Solid>
      </bldg:lod2Solid>
    </bldg:Building>
  </core:cityObjectMember>
  <core:cityObjectMember>
    <bldg:Building gml:id="UUID_not-in-the-graph">
      <bldg:boundedBy>
        <bldg:WallSurface gml:id="OTHER_WALL">
          <bldg:lod2MultiSurface>
            <gml:MultiSurface gml:id="OTHER_WALL_MS">
              <gml:surfaceMember>
                <gml:Polygon gml:id="OTHER_WALL_POLY">
                  <gml:exterior>
                    <gml:LinearRing gml:id="OTHER_WALL_RING">
                      <gml:posList srsDimension="3">457870 5439083 115 457860 5439083 115 457860 5439083 111.8 457870 5439083 111.8 457870 5439083 115</gml:posList>
                    </gml:LinearRing>
                  </gml:exterior>
                </gml:Polygon>
              </gml:surfaceMember>
            </gml:MultiSurface>
          </bldg:lod2MultiSurface>
        </bldg:WallSurface>
      </bldg:boundedBy>
      <bldg:lod2Solid>
        <gml:Solid gml:id="OTHER_SOLID">
          <gml:exterior>
            <gml:CompositeSurface gml:id="OTHER_SHELL">
              <gml:surfaceMember xlink:href="#OTHER_WALL_POLY"/>
            </gml:CompositeSurface>
          </gml:exterior>
        </gml:Solid>
      </bldg:lod2Solid>
    </bldg:Building>
  </core:cityObjectMember>
</core:CityModel>
